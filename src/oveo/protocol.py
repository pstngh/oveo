from __future__ import annotations

import codecs
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

BlockType = Literal["conversation", "deliverable", "advice"]
AppendSeparator = Literal["none", "space", "line", "paragraph"]
EventType = Literal[
    "response_start",
    "block_start",
    "block_delta",
    "block_end",
    "state",
    "response_end",
]
_BLOCK_TYPES = frozenset({"conversation", "deliverable", "advice"})
_BLOCK_ID = re.compile(r"b[1-9][0-9]*\Z")


class ProtocolError(ValueError):
    """A content-free protocol failure safe to expose in operational logs."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"typed response protocol violation: {code}")


@dataclass(frozen=True, slots=True)
class ContentBlock:
    type: BlockType
    text: str


@dataclass(frozen=True, slots=True)
class NoState:
    operation: Literal["none"] = "none"


@dataclass(frozen=True, slots=True)
class EstablishState:
    source: str
    output: str
    brief: dict[str, object]
    operation: Literal["establish"] = "establish"


@dataclass(frozen=True, slots=True)
class AppendState:
    base_version: int
    source_addition: str
    output_addition: str
    source_separator: AppendSeparator
    output_separator: AppendSeparator
    brief: dict[str, object] | None = None
    operation: Literal["append"] = "append"


@dataclass(frozen=True, slots=True)
class SourceOutputReplacement:
    source_anchor: str
    source_replacement: str
    output_anchor: str
    output_replacement: str


@dataclass(frozen=True, slots=True)
class OutputReplacement:
    output_anchor: str
    output_replacement: str


ExactReplacement = SourceOutputReplacement | OutputReplacement


@dataclass(frozen=True, slots=True)
class ReplaceState:
    base_version: int
    replacements: tuple[ExactReplacement, ...]
    brief: dict[str, object] | None = None
    operation: Literal["replace"] = "replace"


@dataclass(frozen=True, slots=True)
class FullState:
    base_version: int
    output: str
    source: str | None
    brief: dict[str, object] | None
    operation: Literal["full"] = "full"


StateOperation = NoState | EstablishState | AppendState | ReplaceState | FullState


@dataclass(frozen=True, slots=True)
class ProtocolDocument:
    version: Literal[1]
    blocks: tuple[ContentBlock, ...]
    state: StateOperation

    def to_storage(self) -> dict[str, object]:
        return {
            "version": self.version,
            "blocks": [{"type": block.type, "text": block.text} for block in self.blocks],
        }


@dataclass(frozen=True, slots=True)
class ProtocolEvent:
    event: EventType
    block_id: str | None = None
    block_type: BlockType | None = None
    text: str | None = None
    state: StateOperation | None = None


def _reject_constant(_value: str) -> None:
    raise ProtocolError("invalid_json_number")


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ProtocolError("duplicate_json_key")
        output[key] = value
    return output


def _load_object(line: str) -> dict[str, object]:
    try:
        value = json.loads(
            line,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ProtocolError:
        raise
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError) as exc:
        raise ProtocolError("invalid_json") from exc
    if not isinstance(value, dict):
        raise ProtocolError("event_not_object")
    return cast(dict[str, object], value)


def _require_exact_keys(event: Mapping[str, object], expected: frozenset[str]) -> None:
    if frozenset(event) != expected:
        raise ProtocolError("invalid_event_fields")


def _require_keys(
    event: Mapping[str, object],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    actual = frozenset(event)
    if not required.issubset(actual) or not actual.issubset(required | optional):
        raise ProtocolError("invalid_event_fields")


def _validated_base_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2_147_483_647:
        raise ProtocolError("invalid_base_version")
    return value


def _validated_text(
    value: object,
    *,
    allow_empty: bool = False,
    complete: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ProtocolError("invalid_state_text")
    if (not allow_empty and not value) or (complete and not value.strip()):
        raise ProtocolError("invalid_state_text")
    return value


def _validate_json_value(value: object, *, depth: int, nodes: list[int]) -> None:
    nodes[0] += 1
    if depth > 12 or nodes[0] > 4096:
        raise ProtocolError("invalid_state_brief")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError("invalid_state_brief")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1, nodes=nodes)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError("invalid_state_brief")
            _validate_json_value(item, depth=depth + 1, nodes=nodes)
        return
    raise ProtocolError("invalid_state_brief")


def _validated_brief(value: object, *, max_bytes: int) -> dict[str, object]:
    if not isinstance(value, dict) or not value:
        raise ProtocolError("invalid_state_brief")
    _validate_json_value(value, depth=0, nodes=[0])
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolError("invalid_state_brief") from exc
    if len(encoded) > max_bytes:
        raise ProtocolError("state_brief_too_large")
    return cast(dict[str, object], value)


def validate_content_blocks(
    blocks: Sequence[ContentBlock | Mapping[str, object]],
    *,
    max_blocks: int = 16,
    max_text_chars: int = 2_000_000,
) -> tuple[ContentBlock, ...]:
    if not blocks or len(blocks) > max_blocks:
        raise ProtocolError("invalid_block_count")

    validated: list[ContentBlock] = []
    total_chars = 0
    for candidate in blocks:
        if isinstance(candidate, ContentBlock):
            block = candidate
        else:
            _require_exact_keys(candidate, frozenset({"type", "text"}))
            block_type = candidate.get("type")
            text = candidate.get("text")
            if block_type not in _BLOCK_TYPES or not isinstance(block_type, str):
                raise ProtocolError("invalid_block_type")
            if not isinstance(text, str) or not text.strip():
                raise ProtocolError("empty_block")
            block = ContentBlock(type=cast(BlockType, block_type), text=text)
        if block.type not in _BLOCK_TYPES:
            raise ProtocolError("invalid_block_type")
        if not block.text.strip():
            raise ProtocolError("empty_block")
        total_chars += len(block.text)
        if total_chars > max_text_chars:
            raise ProtocolError("response_too_large")
        validated.append(block)

    types = [block.type for block in validated]
    if types == ["conversation"]:
        return tuple(validated)
    if types[0] != "deliverable":
        raise ProtocolError("invalid_block_order")
    advice_seen = False
    for block_type in types:
        if block_type == "conversation":
            raise ProtocolError("invalid_block_order")
        if block_type == "advice":
            if advice_seen:
                raise ProtocolError("invalid_block_order")
            advice_seen = True
        elif advice_seen:
            raise ProtocolError("invalid_block_order")
    return tuple(validated)


def validate_stored_document(value: Mapping[str, object]) -> ProtocolDocument:
    _require_exact_keys(value, frozenset({"version", "blocks"}))
    version = value.get("version")
    if isinstance(version, bool) or version != 1:
        raise ProtocolError("unsupported_version")
    raw_blocks = value.get("blocks")
    if not isinstance(raw_blocks, list):
        raise ProtocolError("invalid_blocks")
    mappings: list[Mapping[str, object]] = []
    for block in raw_blocks:
        if not isinstance(block, dict):
            raise ProtocolError("invalid_blocks")
        mappings.append(cast(dict[str, object], block))
    return ProtocolDocument(
        version=1,
        blocks=validate_content_blocks(mappings),
        state=NoState(),
    )


class ProtocolDecoder:
    """Incrementally decode and validate the protocol's UTF-8 NDJSON stream."""

    def __init__(
        self,
        *,
        max_line_bytes: int = 16_777_216,
        max_blocks: int = 16,
        max_text_chars: int = 2_000_000,
        max_state_text_chars: int = 4_000_000,
        max_brief_bytes: int = 65_536,
        max_replacements: int = 64,
    ) -> None:
        if (
            max_line_bytes < 1
            or max_blocks < 1
            or max_text_chars < 1
            or max_state_text_chars < 1
            or max_brief_bytes < 1
            or max_replacements < 1
        ):
            raise ValueError("protocol limits must be positive")
        self._utf8 = codecs.getincrementaldecoder("utf-8")("strict")
        self._buffer = ""
        self._max_line_bytes = max_line_bytes
        self._max_blocks = max_blocks
        self._max_text_chars = max_text_chars
        self._max_state_text_chars = max_state_text_chars
        self._max_brief_bytes = max_brief_bytes
        self._max_replacements = max_replacements
        self._started = False
        self._completed = False
        self._state_seen = False
        self._state: StateOperation | None = None
        self._active_id: str | None = None
        self._active_type: BlockType | None = None
        self._active_parts: list[str] = []
        self._active_delta_count = 0
        self._blocks: list[ContentBlock] = []
        self._total_chars = 0

    def feed(self, chunk: bytes) -> tuple[ProtocolEvent, ...]:
        if not isinstance(chunk, bytes):
            raise TypeError("protocol chunks must be bytes")
        if self._completed and chunk:
            raise ProtocolError("data_after_response_end")
        try:
            self._buffer += self._utf8.decode(chunk, final=False)
        except UnicodeDecodeError as exc:
            raise ProtocolError("invalid_utf8") from exc
        return self._drain_complete_lines()

    def finish(self) -> ProtocolDocument:
        try:
            self._buffer += self._utf8.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise ProtocolError("invalid_utf8") from exc
        self._drain_complete_lines()
        if self._buffer:
            line = self._buffer
            self._buffer = ""
            self._consume_line(line.removesuffix("\r"))
        if not self._completed:
            raise ProtocolError("incomplete_response")
        if self._state is None:
            raise ProtocolError("missing_state")
        return ProtocolDocument(
            version=1,
            blocks=validate_content_blocks(
                self._blocks,
                max_blocks=self._max_blocks,
                max_text_chars=self._max_text_chars,
            ),
            state=self._state,
        )

    def _drain_complete_lines(self) -> tuple[ProtocolEvent, ...]:
        events: list[ProtocolEvent] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            events.append(self._consume_line(line.removesuffix("\r")))
        if len(self._buffer.encode("utf-8")) > self._max_line_bytes:
            raise ProtocolError("event_too_large")
        return tuple(events)

    def _consume_line(self, line: str) -> ProtocolEvent:
        if not line:
            raise ProtocolError("blank_line")
        if len(line.encode("utf-8")) > self._max_line_bytes:
            raise ProtocolError("event_too_large")
        if self._completed:
            raise ProtocolError("data_after_response_end")

        payload = _load_object(line)
        version = payload.get("v")
        if isinstance(version, bool) or version != 1:
            raise ProtocolError("unsupported_version")
        event_name = payload.get("event")
        if not isinstance(event_name, str):
            raise ProtocolError("invalid_event")

        if self._state_seen and event_name != "response_end":
            if event_name == "state":
                raise ProtocolError("duplicate_state")
            raise ProtocolError("event_after_state")

        if event_name == "response_start":
            _require_exact_keys(payload, frozenset({"v", "event"}))
            if self._started:
                raise ProtocolError("duplicate_response_start")
            self._started = True
            return ProtocolEvent(event="response_start")

        if not self._started:
            raise ProtocolError("missing_response_start")

        if event_name == "block_start":
            _require_exact_keys(payload, frozenset({"v", "event", "id", "type"}))
            if self._active_id is not None:
                raise ProtocolError("interleaved_blocks")
            if len(self._blocks) >= self._max_blocks:
                raise ProtocolError("invalid_block_count")
            block_id = payload.get("id")
            block_type = payload.get("type")
            expected_id = f"b{len(self._blocks) + 1}"
            if (
                not isinstance(block_id, str)
                or _BLOCK_ID.fullmatch(block_id) is None
                or block_id != expected_id
            ):
                raise ProtocolError("invalid_block_id")
            if not isinstance(block_type, str) or block_type not in _BLOCK_TYPES:
                raise ProtocolError("invalid_block_type")
            self._active_id = block_id
            self._active_type = cast(BlockType, block_type)
            self._active_parts = []
            self._active_delta_count = 0
            return ProtocolEvent(
                event="block_start",
                block_id=block_id,
                block_type=cast(BlockType, block_type),
            )

        if event_name == "block_delta":
            _require_exact_keys(payload, frozenset({"v", "event", "id", "text"}))
            block_id = payload.get("id")
            text = payload.get("text")
            if self._active_id is None or block_id != self._active_id:
                raise ProtocolError("delta_without_active_block")
            if not isinstance(text, str) or not text:
                raise ProtocolError("empty_delta")
            self._total_chars += len(text)
            if self._total_chars > self._max_text_chars:
                raise ProtocolError("response_too_large")
            self._active_parts.append(text)
            self._active_delta_count += 1
            return ProtocolEvent(
                event="block_delta",
                block_id=self._active_id,
                block_type=self._active_type,
                text=text,
            )

        if event_name == "block_end":
            _require_exact_keys(payload, frozenset({"v", "event", "id"}))
            block_id = payload.get("id")
            if self._active_id is None or block_id != self._active_id:
                raise ProtocolError("end_without_active_block")
            if (
                self._active_delta_count == 0
                or self._active_type is None
                or not "".join(self._active_parts).strip()
            ):
                raise ProtocolError("empty_block")
            block_type = self._active_type
            self._blocks.append(ContentBlock(type=block_type, text="".join(self._active_parts)))
            self._active_id = None
            self._active_type = None
            self._active_parts = []
            self._active_delta_count = 0
            return ProtocolEvent(event="block_end", block_id=block_id)

        if event_name == "state":
            if self._active_id is not None:
                raise ProtocolError("state_with_open_block")
            blocks = validate_content_blocks(
                self._blocks,
                max_blocks=self._max_blocks,
                max_text_chars=self._max_text_chars,
            )
            state = self._decode_state(payload)
            if blocks[0].type == "conversation" and not isinstance(state, NoState):
                raise ProtocolError("invalid_state_for_conversation")
            self._state = state
            self._state_seen = True
            return ProtocolEvent(event="state", state=state)

        if event_name == "response_end":
            _require_exact_keys(payload, frozenset({"v", "event"}))
            if self._active_id is not None:
                raise ProtocolError("response_ended_with_open_block")
            if not self._state_seen:
                raise ProtocolError("missing_state")
            validate_content_blocks(
                self._blocks,
                max_blocks=self._max_blocks,
                max_text_chars=self._max_text_chars,
            )
            self._completed = True
            return ProtocolEvent(event="response_end")

        raise ProtocolError("unknown_event")

    def _decode_state(self, payload: Mapping[str, object]) -> StateOperation:
        operation = payload.get("operation")
        if not isinstance(operation, str):
            raise ProtocolError("invalid_state_operation")

        if operation == "none":
            _require_exact_keys(payload, frozenset({"v", "event", "operation"}))
            return NoState()

        if operation == "establish":
            _require_exact_keys(
                payload,
                frozenset({"v", "event", "operation", "source", "output", "brief"}),
            )
            source = _validated_text(payload.get("source"), complete=True)
            output = _validated_text(payload.get("output"), complete=True)
            brief = _validated_brief(payload.get("brief"), max_bytes=self._max_brief_bytes)
            self._validate_state_size(source, output)
            return EstablishState(source=source, output=output, brief=brief)

        if operation == "append":
            _require_keys(
                payload,
                required=frozenset(
                    {
                        "v",
                        "event",
                        "operation",
                        "base_version",
                        "source_addition",
                        "output_addition",
                        "source_separator",
                        "output_separator",
                    }
                ),
                optional=frozenset({"brief"}),
            )
            base_version = _validated_base_version(payload.get("base_version"))
            source_addition = _validated_text(payload.get("source_addition"), complete=True)
            output_addition = _validated_text(payload.get("output_addition"), complete=True)
            source_separator = self._validated_append_separator(payload.get("source_separator"))
            output_separator = self._validated_append_separator(payload.get("output_separator"))
            append_brief = (
                _validated_brief(payload.get("brief"), max_bytes=self._max_brief_bytes)
                if "brief" in payload
                else None
            )
            self._validate_state_size(source_addition, output_addition)
            return AppendState(
                base_version=base_version,
                source_addition=source_addition,
                output_addition=output_addition,
                source_separator=source_separator,
                output_separator=output_separator,
                brief=append_brief,
            )

        if operation == "replace":
            _require_keys(
                payload,
                required=frozenset({"v", "event", "operation", "base_version", "replacements"}),
                optional=frozenset({"brief"}),
            )
            base_version = _validated_base_version(payload.get("base_version"))
            raw_replacements = payload.get("replacements")
            if (
                not isinstance(raw_replacements, list)
                or not raw_replacements
                or len(raw_replacements) > self._max_replacements
            ):
                raise ProtocolError("invalid_replacement_count")
            replacements: list[ExactReplacement] = []
            seen_source_anchors: set[str] = set()
            seen_output_anchors: set[str] = set()
            state_texts: list[str] = []
            for raw_replacement in raw_replacements:
                if not isinstance(raw_replacement, dict):
                    raise ProtocolError("invalid_replacement")
                keys = frozenset(raw_replacement)
                output_keys = frozenset({"output_anchor", "output_replacement"})
                paired_keys = output_keys | frozenset({"source_anchor", "source_replacement"})
                if keys not in {output_keys, paired_keys}:
                    raise ProtocolError("invalid_replacement_fields")
                output_anchor = _validated_text(raw_replacement.get("output_anchor"))
                output_replacement = _validated_text(
                    raw_replacement.get("output_replacement"), allow_empty=True
                )
                if output_anchor in seen_output_anchors:
                    raise ProtocolError("duplicate_replacement_anchor")
                seen_output_anchors.add(output_anchor)
                state_texts.extend((output_anchor, output_replacement))
                if keys == output_keys:
                    replacements.append(
                        OutputReplacement(
                            output_anchor=output_anchor,
                            output_replacement=output_replacement,
                        )
                    )
                    continue
                source_anchor = _validated_text(raw_replacement.get("source_anchor"))
                source_replacement = _validated_text(
                    raw_replacement.get("source_replacement"), allow_empty=True
                )
                if source_anchor in seen_source_anchors:
                    raise ProtocolError("duplicate_replacement_anchor")
                seen_source_anchors.add(source_anchor)
                state_texts.extend((source_anchor, source_replacement))
                replacements.append(
                    SourceOutputReplacement(
                        source_anchor=source_anchor,
                        source_replacement=source_replacement,
                        output_anchor=output_anchor,
                        output_replacement=output_replacement,
                    )
                )
            self._validate_state_size(*state_texts)
            replacement_brief = (
                _validated_brief(payload.get("brief"), max_bytes=self._max_brief_bytes)
                if "brief" in payload
                else None
            )
            return ReplaceState(
                base_version=base_version,
                replacements=tuple(replacements),
                brief=replacement_brief,
            )

        if operation == "full":
            _require_keys(
                payload,
                required=frozenset({"v", "event", "operation", "base_version", "output"}),
                optional=frozenset({"source", "brief"}),
            )
            base_version = _validated_base_version(payload.get("base_version"))
            output = _validated_text(payload.get("output"), complete=True)
            full_source_raw = payload.get("source")
            full_source = (
                _validated_text(full_source_raw, complete=True) if "source" in payload else None
            )
            full_brief = (
                _validated_brief(payload.get("brief"), max_bytes=self._max_brief_bytes)
                if "brief" in payload
                else None
            )
            self._validate_state_size(
                output,
                *(value for value in (full_source,) if value is not None),
            )
            return FullState(
                base_version=base_version,
                output=output,
                source=full_source,
                brief=full_brief,
            )

        raise ProtocolError("invalid_state_operation")

    def _validate_state_size(self, *values: str) -> None:
        if sum(len(value) for value in values) > self._max_state_text_chars:
            raise ProtocolError("state_too_large")

    @staticmethod
    def _validated_append_separator(value: object) -> AppendSeparator:
        if not isinstance(value, str) or value not in {
            "none",
            "space",
            "line",
            "paragraph",
        }:
            raise ProtocolError("invalid_append_separator")
        return cast(AppendSeparator, value)


__all__ = [
    "AppendSeparator",
    "AppendState",
    "BlockType",
    "ContentBlock",
    "EstablishState",
    "ExactReplacement",
    "FullState",
    "NoState",
    "OutputReplacement",
    "ProtocolDecoder",
    "ProtocolDocument",
    "ProtocolError",
    "ProtocolEvent",
    "ReplaceState",
    "SourceOutputReplacement",
    "StateOperation",
    "validate_content_blocks",
    "validate_stored_document",
]
