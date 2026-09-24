from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
from collections import OrderedDict
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
_PROVIDER_ROUTING: dict[str, object] = {
    "order": ["azure/eu"],
    "allow_fallbacks": True,
    "data_collection": "deny",
    "zdr": True,
}

# Supported reasoning levels for GPT-5.6 Luna; keep effort explicit per workload.
ReasoningEffort = Literal["max", "xhigh", "high", "medium", "low", "none"]
_REASONING_EFFORTS: frozenset[str] = frozenset({"max", "xhigh", "high", "medium", "low", "none"})

Role = Literal["system", "user", "assistant"]
DeltaCallback = Callable[[str], Awaitable[None] | None]
# Called once per attempt with (x-request-id, OpenRouter generation id) as soon as the
# generation id is known, so an abandoned or failed call can still be reconciled.
IdsCallback = Callable[[str | None, str], Awaitable[None]]
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
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderCompletion:
    text: str
    provider_request_id: str | None
    provider_generation_id: str | None
    provider_name: str | None
    usage: ProviderUsage | None


@lru_cache(maxsize=1)
def _model_encoding() -> Encoding:
    # GPT-5.6 Luna maps to o200k_base in the pinned tiktoken version.
    # Provider serialization still adds framing to these local estimates.
    return tiktoken.get_encoding("o200k_base")


def warm_tokenizer() -> None:
    """Load the model tokenizer before the server begins accepting requests."""

    _model_encoding()


_TOKEN_CACHE_SIZE = 64
_token_cache: OrderedDict[bytes, int] = OrderedDict()
_token_cache_lock = threading.Lock()


def count_text_tokens(text: str) -> int:
    """Estimate one text's tokens, reusing counts for identical text.

    Compaction and preflight checks count the same system prompt and transcript
    envelopes repeatedly. Entries are keyed by a SHA-256 digest so the cache never
    holds conversation text itself.
    """

    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()
    with _token_cache_lock:
        cached = _token_cache.get(digest)
        if cached is not None:
            _token_cache.move_to_end(digest)
            return cached
    count = len(_model_encoding().encode(text, disallowed_special=()))
    with _token_cache_lock:
        _token_cache[digest] = count
        _token_cache.move_to_end(digest)
        while len(_token_cache) > _TOKEN_CACHE_SIZE:
            _token_cache.popitem(last=False)
    return count


def count_input_tokens(messages: Sequence[ProviderMessage]) -> int:
    """Count model input tokens with a small allowance for chat framing.

    OpenRouter does not expose OpenAI's preflight input-token endpoint. The content and
    roles use o200k_base as an estimate. The framing allowance is conservative,
    and the default 32K-token margin absorbs provider serialization differences.
    """

    total = 3  # Assistant reply priming.
    for message in messages:
        total += 4  # Per-message role and framing markers.
        total += count_text_tokens(message.role)
        total += count_text_tokens(message.content)
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
    details = value.get("completion_tokens_details")
    usage = ProviderUsage(
        input_tokens=_nonnegative_int(value.get("prompt_tokens")),
        output_tokens=_nonnegative_int(value.get("completion_tokens")),
        total_tokens=_nonnegative_int(value.get("total_tokens")),
        cost_usd=cost_usd,
        cost_microusd=cost_microusd,
        reasoning_tokens=(
            _nonnegative_int(details.get("reasoning_tokens"))
            if isinstance(details, Mapping)
            else None
        ),
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
        # The model is pinned in code (OPENROUTER_MODEL); there is no setting to change it.
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
        reasoning_effort: ReasoningEffort = "high",
    ) -> dict[str, object]:
        if not messages:
            raise ValueError("at least one provider message is required")
        if max_completion_tokens < 1:
            raise ValueError("max_completion_tokens must be positive")
        if reasoning_effort not in _REASONING_EFFORTS:
            raise ValueError("unsupported reasoning effort")
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
            "reasoning_effort": reasoning_effort,
            "provider": dict(_PROVIDER_ROUTING),
        }

    async def stream_chat(
        self,
        messages: Sequence[ProviderMessage],
        *,
        max_completion_tokens: int,
        on_delta: DeltaCallback,
        reasoning_effort: ReasoningEffort = "high",
        on_ids: IdsCallback | None = None,
        deadline_seconds: float | None = None,
    ) -> ProviderCompletion:
        body = self.build_request_body(
            messages,
            max_completion_tokens=max_completion_tokens,
            reasoning_effort=reasoning_effort,
        )
        deadline = deadline_seconds or self._settings.provider_attempt_deadline_seconds
        attempts = self._settings.provider_retry_attempts
        for attempt in range(attempts):
            try:
                return await self._stream_once(
                    body, on_delta=on_delta, on_ids=on_ids, deadline_seconds=deadline
                )
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
                timeout=self._settings.provider_metadata_timeout_seconds,
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
        on_ids: IdsCallback | None,
        deadline_seconds: float,
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
        finish_reason: str | None = None
        done = False

        def failure(code: str, *, retryable: bool) -> ProviderError:
            return ProviderError(
                code,
                retryable=retryable,
                partial=emitted,
                provider_request_id=request_id,
                provider_generation_id=generation_id,
                provider_name=provider_name,
                usage=usage,
            )

        try:
            # OpenRouter keep-alive comments reset httpx's read timeout, so bound the whole
            # attempt as well. Leaving the stream context aborts the upstream request.
            async with (
                asyncio.timeout(deadline_seconds),
                self._client.stream("POST", url, json=body, headers=headers) as response,
            ):
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
                    first_generation_id = generation_id is None
                    generation_id = _safe_string(payload.get("id")) or generation_id
                    if first_generation_id and generation_id is not None and on_ids is not None:
                        await on_ids(request_id, generation_id)
                    provider_name = _safe_string(payload.get("provider")) or provider_name
                    parsed_usage = parse_usage(payload.get("usage"))
                    if parsed_usage is not None:
                        usage = parsed_usage
                    if "error" in payload:
                        transient = _is_transient_code(_error_code(payload.get("error")))
                        raise failure(
                            "provider_transient" if transient else "provider_rejected",
                            retryable=transient,
                        )
                    finish_reason = self._finish_reason(payload) or finish_reason
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
        except TimeoutError as exc:
            # A stalled attempt is not replayed automatically: it may already be billed.
            raise failure("provider_timeout", retryable=False) from exc
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise failure("provider_network", retryable=True) from exc

        # A truncated or filtered response is final; replaying it would bill the same
        # oversized work again. The caller reports it instead of treating it as a
        # protocol error.
        if finish_reason == "length":
            raise failure("provider_output_limit", retryable=False)
        if finish_reason == "content_filter":
            raise failure("provider_content_filter", retryable=False)
        if not done:
            raise failure("provider_incomplete_stream", retryable=True)
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
    def _finish_reason(payload: Mapping[str, object]) -> str | None:
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return None
        for choice in choices:
            if isinstance(choice, Mapping):
                reason = choice.get("finish_reason")
                if isinstance(reason, str) and reason:
                    return reason
        return None

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
