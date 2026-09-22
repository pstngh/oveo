import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from oveo.config import Settings
from oveo.provider import (
    OPENROUTER_MODEL,
    OpenRouterClient,
    ProviderError,
    ProviderMessage,
    count_input_tokens,
    parse_usage,
)


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": tmp_path,
        "attachments_dir": tmp_path / "attachments",
        "openrouter_api_key": SecretStr("not-a-real-provider-key"),
        "provider_retry_attempts": 3,
    }
    values.update(overrides)
    return Settings(**values)


def sse(payload: dict[str, object]) -> bytes:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


def test_input_token_count_uses_luna_tokenizer_and_chat_framing() -> None:
    short = count_input_tokens([ProviderMessage("user", "hello")])
    longer = count_input_tokens([ProviderMessage("user", "hello " * 100)])

    assert short > 1
    assert longer > short
    assert longer < len("hello " * 100)


@pytest.mark.asyncio
async def test_streaming_request_locks_model_privacy_and_parses_actual_cost(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {
            "model": "openai/gpt-5.6-luna",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "synthetic request"},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": 2048,
            "reasoning_effort": "high",
            "provider": {
                "order": ["azure/eu"],
                "allow_fallbacks": True,
                "data_collection": "deny",
                "zdr": True,
            },
        }
        assert "usage" not in body
        content = b"".join(
            (
                sse(
                    {
                        "id": "generation-1",
                        "provider": "azure/eu",
                        "choices": [{"delta": {"content": "first"}}],
                    }
                ),
                sse(
                    {
                        "id": "generation-1",
                        "provider": "azure/eu",
                        "choices": [{"delta": {"content": " second"}}],
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 3,
                            "total_tokens": 15,
                            "cost": "0.000321",
                        },
                    }
                ),
                b"data: [DONE]\n\n",
            )
        )
        return httpx.Response(200, content=content, headers={"x-request-id": "request-1"})

    deltas: list[str] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = OpenRouterClient(settings(tmp_path), client=http_client)
        result = await provider.stream_chat(
            [ProviderMessage("system", "system"), ProviderMessage("user", "synthetic request")],
            max_completion_tokens=2048,
            on_delta=deltas.append,
        )

    assert deltas == ["first", " second"]
    assert result.text == "first second"
    assert result.provider_request_id == "request-1"
    assert result.provider_generation_id == "generation-1"
    assert result.provider_name == "azure/eu"
    assert result.usage is not None
    assert result.usage.cost_usd == Decimal("0.000321")
    assert result.usage.cost_microusd == 321


@pytest.mark.asyncio
async def test_transient_http_failure_retries_before_first_delta(tmp_path: Path) -> None:
    calls = 0
    delays: list[float] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, content=b"private provider message")
        return httpx.Response(
            200,
            content=sse({"id": "generation-2", "choices": [{"delta": {"content": "ok"}}]})
            + b"data: [DONE]\n\n",
        )

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = OpenRouterClient(settings(tmp_path), client=http_client, sleep=fake_sleep)
        result = await provider.stream_chat(
            [ProviderMessage("user", "synthetic")],
            max_completion_tokens=100,
            on_delta=lambda _delta: None,
        )

    assert calls == 2
    assert delays == [1.0]
    assert result.text == "ok"


@pytest.mark.asyncio
async def test_partial_incomplete_stream_is_not_automatically_replayed(tmp_path: Path) -> None:
    calls = 0
    marker = "sensitive-provider-body"

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=sse(
                {
                    "id": "generation-3",
                    "choices": [{"delta": {"content": "partial"}}],
                    "usage": {"cost": "0.000002"},
                    "ignored": marker,
                }
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = OpenRouterClient(settings(tmp_path), client=http_client)
        with pytest.raises(ProviderError) as caught:
            await provider.stream_chat(
                [ProviderMessage("user", "synthetic")],
                max_completion_tokens=100,
                on_delta=lambda _delta: None,
            )

    assert calls == 1
    assert caught.value.code == "provider_incomplete_stream"
    assert caught.value.retryable is True
    assert caught.value.partial is True
    assert caught.value.usage is not None
    assert caught.value.usage.cost_microusd == 2
    assert marker not in str(caught.value)


@pytest.mark.asyncio
async def test_generation_metadata_uses_reported_total_cost(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/generation")
        assert request.url.params.get("id") == "generation-cost"
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.extensions["timeout"] == {
            "connect": 1.25,
            "read": 1.25,
            "write": 1.25,
            "pool": 1.25,
        }
        return httpx.Response(200, json={"data": {"total_cost": "0.000055"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = OpenRouterClient(
            settings(tmp_path, provider_metadata_timeout_seconds=1.25),
            client=http_client,
        )
        assert await provider.generation_cost("generation-cost") == 55


def test_usage_parser_rejects_estimates_and_invalid_values() -> None:
    assert parse_usage({"cost": "NaN"}) is None
    usage = parse_usage({"prompt_tokens": 2, "completion_tokens": 1, "cost": "0.0000015"})
    assert usage is not None
    assert usage.total_tokens is None
    assert usage.cost_microusd == 2


def test_model_is_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=OPENROUTER_MODEL):
        OpenRouterClient(settings(tmp_path, openrouter_model="another/model"))
