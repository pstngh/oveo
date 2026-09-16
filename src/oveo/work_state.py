from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum

MAX_SOURCE_WORDS = 25_000
_WORD = re.compile(
    r"[^\W_]+(?:[_" + "\N{RIGHT SINGLE QUOTATION MARK}" + r"'][^\W_]+)*",
    re.UNICODE,
)


class WorkStateError(ValueError):
    pass


class WordLimitExceeded(WorkStateError):
    def __init__(self, measured_words: int, limit: int = MAX_SOURCE_WORDS) -> None:
        self.measured_words = measured_words
        self.limit = limit
        super().__init__(
            f"Source contains {measured_words:,} words; the maximum is {limit:,} words."
        )


class ExactAnchorError(WorkStateError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"canonical replacement failed: {code}")


class Direction(StrEnum):
    FR_TO_EN_US = "fr-en-US"
    EN_US_TO_FR_CA = "en-US-fr-CA"
    EN_US_TO_FR_FR = "en-US-fr-FR"
    EN_US_TO_FR_INTL = "en-US-fr-INTL"


@dataclass(frozen=True, slots=True)
class CanonicalWorkState:
    source: str
    output: str
    direction: Direction
    brief: str
    version: int
    source_word_count: int


def count_words(text: str) -> int:
    return sum(1 for _match in _WORD.finditer(text))


def _validate_complete_text(label: str, text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise WorkStateError(f"{label} must contain text")


def _validated_word_count(source: str, *, limit: int) -> int:
    measured = count_words(source)
    if measured > limit:
        raise WordLimitExceeded(measured, limit)
    return measured


def establish(
    *,
    source: str,
    output: str,
    direction: Direction,
    brief: str,
    word_limit: int = MAX_SOURCE_WORDS,
) -> CanonicalWorkState:
    _validate_complete_text("source", source)
    _validate_complete_text("output", output)
    _validate_complete_text("brief", brief)
    if not isinstance(direction, Direction):
        raise WorkStateError("direction is unsupported")
    if word_limit < 1:
        raise ValueError("word limit must be positive")
    return CanonicalWorkState(
        source=source,
        output=output,
        direction=direction,
        brief=brief,
        version=1,
        source_word_count=_validated_word_count(source, limit=word_limit),
    )


def append(
    state: CanonicalWorkState,
    *,
    source_addition: str,
    output_addition: str,
    source_separator: str = "\n\n",
    output_separator: str = "\n\n",
    word_limit: int = MAX_SOURCE_WORDS,
) -> CanonicalWorkState:
    _validate_complete_text("source addition", source_addition)
    _validate_complete_text("output addition", output_addition)
    source = f"{state.source}{source_separator}{source_addition}"
    output = f"{state.output}{output_separator}{output_addition}"
    return replace(
        state,
        source=source,
        output=output,
        version=state.version + 1,
        source_word_count=_validated_word_count(source, limit=word_limit),
    )


def _replace_unique(text: str, anchor: str, replacement: str, *, label: str) -> str:
    if not anchor:
        raise ExactAnchorError(f"empty_{label}_anchor")
    first = text.find(anchor)
    if first < 0:
        raise ExactAnchorError(f"missing_{label}_anchor")
    if text.find(anchor, first + 1) >= 0:
        raise ExactAnchorError(f"ambiguous_{label}_anchor")
    return f"{text[:first]}{replacement}{text[first + len(anchor) :]}"


def replace_exact(
    state: CanonicalWorkState,
    *,
    source_anchor: str,
    source_replacement: str,
    output_anchor: str,
    output_replacement: str,
    word_limit: int = MAX_SOURCE_WORDS,
) -> CanonicalWorkState:
    source = _replace_unique(
        state.source,
        source_anchor,
        source_replacement,
        label="source",
    )
    output = _replace_unique(
        state.output,
        output_anchor,
        output_replacement,
        label="output",
    )
    _validate_complete_text("source", source)
    _validate_complete_text("output", output)
    return replace(
        state,
        source=source,
        output=output,
        version=state.version + 1,
        source_word_count=_validated_word_count(source, limit=word_limit),
    )


def replace_output_exact(
    state: CanonicalWorkState,
    *,
    output_anchor: str,
    output_replacement: str,
) -> CanonicalWorkState:
    output = _replace_unique(
        state.output,
        output_anchor,
        output_replacement,
        label="output",
    )
    _validate_complete_text("output", output)
    return replace(state, output=output, version=state.version + 1)


def replace_full(
    state: CanonicalWorkState,
    *,
    output: str,
    source: str | None = None,
    brief: str | None = None,
    word_limit: int = MAX_SOURCE_WORDS,
) -> CanonicalWorkState:
    next_source = state.source if source is None else source
    next_brief = state.brief if brief is None else brief
    _validate_complete_text("source", next_source)
    _validate_complete_text("output", output)
    _validate_complete_text("brief", next_brief)
    return replace(
        state,
        source=next_source,
        output=output,
        brief=next_brief,
        version=state.version + 1,
        source_word_count=_validated_word_count(next_source, limit=word_limit),
    )


def full_current(state: CanonicalWorkState) -> str:
    return state.output
