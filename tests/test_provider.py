import asyncio
import json
from collections import OrderedDict
from collections.abc import AsyncIterator
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
            "model": "openai/gpt-6-luna",
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


def test_model_is_pinned_in_code_even_if_the_environment_names_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OVEO_OPENROUTER_MODEL", "another/model")
    client = OpenRouterClient(settings(tmp_path))
    body = client.build_request_body(
        [ProviderMessage("user", "synthetic")], max_completion_tokens=10, reasoning_effort="low"
    )
    assert body["model"] == OPENROUTER_MODEL == "openai/gpt-6-luna"
    assert body["reasoning_effort"] == "low"
    assert body["provider"] == {
        "order": ["azure/eu"],
        "allow_fallbacks": True,
        "data_collection": "deny",
        "zdr": True,
    }
    with pytest.raises(ValueError, match="reasoning effort"):
        client.build_request_body(
            [ProviderMessage("user", "synthetic")],
            max_completion_tokens=10,
            reasoning_effort="minimal",  # type: ignore[arg-type]
        )


def _finishing_stream(finish_reason: str) -> bytes:
    return b"".join(
        (
            sse(
                {
                    "id": "generation-cut",
                    "choices": [{"delta": {"content": "partial"}, "finish_reason": None}],
                }
            ),
            sse(
                {
                    "id": "generation-cut",
                    "choices": [{"delta": {"content": ""}, "finish_reason": finish_reason}],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 32,
                        "completion_tokens_details": {"reasoning_tokens": 30},
                        "cost": "0.000009",
                    },
                }
            ),
            b"data: [DONE]\n\n",
        )
    )


@pytest.mark.parametrize(
    ("finish_reason", "code"),
    [("length", "provider_output_limit"), ("content_filter", "provider_content_filter")],
)
@pytest.mark.asyncio
async def test_truncated_or_filtered_response_is_final_and_keeps_usage(
    tmp_path: Path, finish_reason: str, code: str
) -> None:
    calls = 0
    ids: list[tuple[str | None, str]] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, content=_finishing_stream(finish_reason), headers={"x-request-id": "req-cut"}
        )

    async def on_ids(request_id: str | None, generation_id: str) -> None:
        ids.append((request_id, generation_id))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = OpenRouterClient(settings(tmp_path), client=http_client)
        with pytest.raises(ProviderError) as caught:
            await provider.stream_chat(
                [ProviderMessage("user", "synthetic")],
                max_completion_tokens=32,
                on_delta=lambda _delta: None,
                on_ids=on_ids,
            )

    assert calls == 1  # never replayed, although three attempts are configured
    assert caught.value.code == code
    assert caught.value.retryable is False
    assert caught.value.provider_generation_id == "generation-cut"
    assert caught.value.usage is not None
    assert caught.value.usage.cost_microusd == 9
    assert caught.value.usage.reasoning_tokens == 30
    assert ids == [("req-cut", "generation-cut")]


@pytest.mark.asyncio
async def test_provider_ids_are_reported_before_a_stream_fails(tmp_path: Path) -> None:
    ids: list[tuple[str | None, str]] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        # One content chunk, then the connection ends without [DONE].
        return httpx.Response(
            200,
            content=sse({"id": "generation-early", "choices": [{"delta": {"content": "x"}}]}),
            headers={"x-request-id": "req-early"},
        )

    async def on_ids(request_id: str | None, generation_id: str) -> None:
        ids.append((request_id, generation_id))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = OpenRouterClient(settings(tmp_path), client=http_client)
        with pytest.raises(ProviderError, match="provider_incomplete_stream"):
            await provider.stream_chat(
                [ProviderMessage("user", "synthetic")],
                max_completion_tokens=32,
                on_delta=lambda _delta: None,
                on_ids=on_ids,
            )
    assert ids == [("req-early", "generation-early")]


@pytest.mark.asyncio
async def test_keep_alives_cannot_extend_an_attempt_past_its_deadline(tmp_path: Path) -> None:
    calls = 0

    async def keep_alive_forever() -> AsyncIterator[bytes]:
        yield sse({"id": "generation-stalled", "choices": [{"delta": {"content": "x"}}]})
        while True:
            await asyncio.sleep(0.02)
            yield b": OPENROUTER PROCESSING\n\n"

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=keep_alive_forever())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = OpenRouterClient(settings(tmp_path), client=http_client)
        started = asyncio.get_running_loop().time()
        with pytest.raises(ProviderError) as caught:
            await provider.stream_chat(
                [ProviderMessage("user", "synthetic")],
                max_completion_tokens=32,
                on_delta=lambda _delta: None,
                deadline_seconds=0.3,
            )
        elapsed = asyncio.get_running_loop().time() - started

    assert caught.value.code == "provider_timeout"
    assert caught.value.retryable is False
    assert caught.value.partial is True
    assert calls == 1
    assert elapsed < 3


def test_token_counts_are_cached_under_a_digest_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # L-6: compaction and preflight count the same prompt and transcript text repeatedly.
    import oveo.provider as provider_module

    counted: list[str] = []

    class CountingEncoding:
        def encode(self, text: str, disallowed_special: object = ()) -> list[int]:
            counted.append(text)
            return [0] * len(text.split())

    monkeypatch.setattr(provider_module, "_model_encoding", CountingEncoding)
    monkeypatch.setattr(provider_module, "_token_cache", OrderedDict())
    text = "Synthetic transcript text. " * 40

    assert provider_module.count_text_tokens(text) == 120
    assert provider_module.count_text_tokens(text) == 120
    assert counted == [text]
    # Keys are SHA-256 digests: the cache never holds conversation text.
    assert [len(key) for key in provider_module._token_cache] == [32]
