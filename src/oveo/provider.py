from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from typing import Literal, cast

import httpx
import tiktoken
from tiktoken import Encoding

from .config import Settings

OPENROUTER_MODEL = "openai/gpt-5.6-luna"
OPENROUTER_PROVIDER_ROUTING: dict[str, object] = {
    "order": ["azure/eu"],
    "allow_fallbacks": True,
    "data_collection": "deny",
    "zdr": True,
}

Role = Literal["system", "user", "assistant"]
DeltaCallback = Callable[[str], Awaitable[None] | None]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ProviderMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost_usd: Decimal | None
    cost_microusd: int | None


@dataclass(frozen=True, slots=True)
class ProviderCompletion:
    text: str
    provider_request_id: str | None
    provider_generation_id: str | None
    provider_name: str | None
    usage: ProviderUsage | None


@lru_cache(maxsize=1)
def _model_encoding() -> Encoding:
    return tiktoken.encoding_for_model(OPENROUTER_MODEL.partition("/")[2])


def count_input_tokens(messages: Sequence[ProviderMessage]) -> int:
    """Count model input tokens with a small allowance for chat framing.

    OpenRouter does not expose OpenAI's preflight input-token endpoint. The content and
    roles use Luna's model tokenizer exactly; the framing allowance is deliberately
    conservative and the default 32K-token margin absorbs provider serialization
    differences.
    """

    encoding = _model_encoding()
    total = 3  # Assistant reply priming.
    for message in messages:
        total += 4  # Per-message role and framing markers.
        total += len(encoding.encode(message.role, disallowed_special=()))
        total += len(encoding.encode(message.content, disallowed_special=()))
    return total


class ProviderError(RuntimeError):
    """A scrubbed provider failure that never retains a response or prompt body."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        partial: bool = False,
        provider_request_id: str | None = None,
        provider_generation_id: str | None = None,
        provider_name: str | None = None,
        usage: ProviderUsage | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.partial = partial
        self.provider_request_id = provider_request_id
        self.provider_generation_id = provider_generation_id
        self.provider_name = provider_name
        self.usage = usage
        super().__init__(f"OpenRouter request failed: {code}")

    def with_partial(self) -> ProviderError:
        return ProviderError(
            self.code,
            retryable=self.retryable,
            status_code=self.status_code,
            partial=True,
            provider_request_id=self.provider_request_id,
            provider_generation_id=self.provider_generation_id,
            provider_name=self.provider_name,
            usage=self.usage,
        )


def _safe_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _cost(value: object) -> tuple[Decimal | None, int | None]:
    if value is None or isinstance(value, bool):
        return None, None
    if not isinstance(value, (str, int, float, Decimal)):
        return None, None
    try:
        dollars = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None, None
    if not dollars.is_finite() or dollars < 0:
        return None, None
    microusd = int((dollars * Decimal(1_000_000)).to_integral_value(rounding=ROUND_HALF_UP))
    return dollars, microusd


def parse_usage(value: object) -> ProviderUsage | None:
    if not isinstance(value, Mapping):
        return None
    cost_usd, cost_microusd = _cost(value.get("cost"))
    usage = ProviderUsage(
        input_tokens=_nonnegative_int(value.get("prompt_tokens")),
        output_tokens=_nonnegative_int(value.get("completion_tokens")),
        total_tokens=_nonnegative_int(value.get("total_tokens")),
        cost_usd=cost_usd,
        cost_microusd=cost_microusd,
    )
    if all(
        field is None
        for field in (
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
            usage.cost_usd,
        )
    ):
        return None
    return usage


def _error_code(value: object) -> object:
    if isinstance(value, Mapping):
        return value.get("code")
    return None


def _is_transient_code(code: object) -> bool:
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    if isinstance(code, int):
        return code in {408, 409, 425, 429} or code >= 500
    return code in {
        "request_timeout",
        "rate_limit_exceeded",
        "server_error",
        "provider_unavailable",
        "overloaded_error",
    }


def _http_error(status_code: int, *, request_id: str | None) -> ProviderError:
    retryable = status_code in {408, 409, 425, 429} or status_code >= 500
    code = "provider_transient" if retryable else "provider_rejected"
    return ProviderError(
        code,
        retryable=retryable,
        status_code=status_code,
        provider_request_id=request_id,
    )


class OpenRouterClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if settings.openrouter_model != OPENROUTER_MODEL:
            raise ValueError(f"OpenRouter model must remain {OPENROUTER_MODEL}")
        if settings.openrouter_api_key is None:
            raise ProviderError("provider_not_configured", retryable=False)
        self._settings = settings
        self._api_key = settings.openrouter_api_key.get_secret_value()
        self._client = client or httpx.AsyncClient(timeout=settings.openrouter_timeout_seconds)
        self._owns_client = client is None
        self._sleep = sleep

    async def __aenter__(self) -> OpenRouterClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def build_request_body(
        self,
        messages: Sequence[ProviderMessage],
        *,
        max_completion_tokens: int,
    ) -> dict[str, object]:
        if not messages:
            raise ValueError("at least one provider message is required")
        if max_completion_tokens < 1:
            raise ValueError("max_completion_tokens must be positive")
        serialized: list[dict[str, str]] = []
        for message in messages:
            if message.role not in {"system", "user", "assistant"}:
                raise ValueError("unsupported provider message role")
            if not message.content:
                raise ValueError("provider message content cannot be empty")
            serialized.append({"role": message.role, "content": message.content})
        return {
            "model": OPENROUTER_MODEL,
            "messages": serialized,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": max_completion_tokens,
            "provider": {
                "order": ["azure/eu"],
                "allow_fallbacks": True,
                "data_collection": "deny",
                "zdr": True,
            },
        }

    async def stream_chat(
        self,
        messages: Sequence[ProviderMessage],
        *,
        max_completion_tokens: int,
        on_delta: DeltaCallback,
    ) -> ProviderCompletion:
        body = self.build_request_body(
            messages,
            max_completion_tokens=max_completion_tokens,
        )
        attempts = self._settings.provider_retry_attempts
        for attempt in range(attempts):
            try:
                return await self._stream_once(body, on_delta=on_delta)
            except ProviderError as exc:
                if not exc.retryable or exc.partial or attempt + 1 >= attempts:
                    raise
                await self._sleep(float(2**attempt))
        raise AssertionError("provider retry loop did not return or raise")

    async def generation_cost(self, provider_generation_id: str) -> int | None:
        """Fetch OpenRouter's eventual account charge without retaining response data."""

        if not provider_generation_id or len(provider_generation_id) > 255:
            raise ValueError("invalid provider generation id")
        url = f"{self._settings.openrouter_base_url.rstrip('/')}/generation"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        try:
            response = await self._client.get(
                url,
                params={"id": provider_generation_id},
                headers=headers,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise ProviderError("provider_network", retryable=True) from exc
        if response.status_code == 404:
            return None
        if response.status_code < 200 or response.status_code >= 300:
            raise _http_error(
                response.status_code,
                request_id=_safe_string(response.headers.get("x-request-id")),
            )
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeError, ValueError, TypeError) as exc:
            raise ProviderError("provider_malformed_metadata", retryable=True) from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), Mapping):
            raise ProviderError("provider_malformed_metadata", retryable=True)
        data = cast(Mapping[str, object], payload["data"])
        _dollars, microusd = _cost(data.get("total_cost"))
        if microusd is not None:
            return microusd
        usage = parse_usage(data.get("usage"))
        return usage.cost_microusd if usage is not None else None

    async def _stream_once(
        self,
        body: Mapping[str, object],
        *,
        on_delta: DeltaCallback,
    ) -> ProviderCompletion:
        url = f"{self._settings.openrouter_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Title": "Oveo",
        }
        emitted = False
        parts: list[str] = []
        request_id: str | None = None
        generation_id: str | None = None
        provider_name: str | None = None
        usage: ProviderUsage | None = None
        done = False

        try:
            async with self._client.stream("POST", url, json=body, headers=headers) as response:
                request_id = _safe_string(response.headers.get("x-request-id"))
                if response.status_code < 200 or response.status_code >= 300:
                    raise _http_error(response.status_code, request_id=request_id)
                async for line in response.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].lstrip()
                    if data == "[DONE]":
                        done = True
                        break
                    payload = self._decode_stream_payload(data)
                    generation_id = _safe_string(payload.get("id")) or generation_id
                    provider_name = _safe_string(payload.get("provider")) or provider_name
                    parsed_usage = parse_usage(payload.get("usage"))
                    if parsed_usage is not None:
                        usage = parsed_usage
                    if "error" in payload:
                        transient = _is_transient_code(_error_code(payload.get("error")))
                        raise ProviderError(
                            "provider_transient" if transient else "provider_rejected",
                            retryable=transient,
                            provider_request_id=request_id,
                            provider_generation_id=generation_id,
                            provider_name=provider_name,
                            usage=usage,
                        )
                    for delta in self._content_deltas(payload):
                        parts.append(delta)
                        emitted = True
                        callback_result = on_delta(delta)
                        if inspect.isawaitable(callback_result):
                            await callback_result
        except ProviderError as exc:
            if emitted and not exc.partial:
                raise exc.with_partial() from exc
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            error = ProviderError(
                "provider_network",
                retryable=True,
                partial=emitted,
                provider_request_id=request_id,
                provider_generation_id=generation_id,
                provider_name=provider_name,
                usage=usage,
            )
            raise error from exc

        if not done:
            raise ProviderError(
                "provider_incomplete_stream",
                retryable=True,
                partial=emitted,
                provider_request_id=request_id,
                provider_generation_id=generation_id,
                provider_name=provider_name,
                usage=usage,
            )
        return ProviderCompletion(
            text="".join(parts),
            provider_request_id=request_id,
            provider_generation_id=generation_id,
            provider_name=provider_name,
            usage=usage,
        )

    @staticmethod
    def _decode_stream_payload(data: str) -> dict[str, object]:
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, UnicodeError, ValueError, TypeError) as exc:
            raise ProviderError("provider_malformed_stream", retryable=False) from exc
        if not isinstance(payload, dict):
            raise ProviderError("provider_malformed_stream", retryable=False)
        return cast(dict[str, object], payload)

    @staticmethod
    def _content_deltas(payload: Mapping[str, object]) -> tuple[str, ...]:
        choices = payload.get("choices")
        if choices is None:
            return ()
        if not isinstance(choices, list):
            raise ProviderError("provider_malformed_stream", retryable=False)
        deltas: list[str] = []
        for choice in choices:
            if not isinstance(choice, Mapping):
                raise ProviderError("provider_malformed_stream", retryable=False)
            delta = choice.get("delta")
            if delta is None:
                continue
            if not isinstance(delta, Mapping):
                raise ProviderError("provider_malformed_stream", retryable=False)
            content = delta.get("content")
            if content is None:
                continue
            if not isinstance(content, str):
                raise ProviderError("provider_malformed_stream", retryable=False)
            if content:
                deltas.append(content)
        return tuple(deltas)
