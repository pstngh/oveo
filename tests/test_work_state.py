import pytest

from oveo.work_state import (
    MAX_SOURCE_WORDS,
    CanonicalWorkState,
    Direction,
    ExactAnchorError,
    WordLimitExceeded,
    append,
    count_words,
    establish,
    full_current,
    replace_exact,
    replace_full,
    replace_output_exact,
)


def initial_state() -> CanonicalWorkState:
    return establish(
        source="Hello world.",
        output="Bonjour le monde.",
        direction=Direction.EN_US_TO_FR_CA,
        brief="Professional Canadian French for employees.",
    )


def test_establish_and_append_create_immutable_complete_versions() -> None:
    original = initial_state()
    updated = append(
        original,
        source_addition="A new paragraph.",
        output_addition="Un nouveau paragraphe.",
    )

    assert original.source == "Hello world."
    assert original.version == 1
    assert updated.source == "Hello world.\n\nA new paragraph."
    assert updated.output == "Bonjour le monde.\n\nUn nouveau paragraphe."
    assert updated.version == 2
    assert updated.source_word_count == 5


def test_word_count_and_cumulative_limit_are_exactly_enforced() -> None:
    assert count_words("L'équipe can't re_use well-supported tools.") == 6
    allowed = " ".join(["word"] * MAX_SOURCE_WORDS)
    state = establish(
        source=allowed,
        output="Complete output.",
        direction=Direction.FR_TO_EN_US,
        brief="Brief.",
    )
    assert state.source_word_count == MAX_SOURCE_WORDS

    with pytest.raises(WordLimitExceeded) as caught:
        append(state, source_addition="one", output_addition="un")
    assert caught.value.measured_words == MAX_SOURCE_WORDS + 1
    assert "25,001" in str(caught.value)


def test_exact_replacement_updates_source_and_output_atomically() -> None:
    original = establish(
        source="First paragraph.\n\nSecond paragraph.",
        output="Premier paragraphe.\n\nDeuxième paragraphe.",
        direction=Direction.EN_US_TO_FR_CA,
        brief="Brief.",
    )
    updated = replace_exact(
        original,
        source_anchor="Second paragraph.",
        source_replacement="Updated paragraph.",
        output_anchor="Deuxième paragraphe.",
        output_replacement="Paragraphe mis à jour.",
    )
    assert updated.source.endswith("Updated paragraph.")
    assert updated.output.endswith("Paragraphe mis à jour.")
    assert updated.version == 2
    assert original.output.endswith("Deuxième paragraphe.")


@pytest.mark.parametrize(
    ("text", "anchor", "code"),
    [
        ("one two", "missing", "missing_output_anchor"),
        ("same and same", "same", "ambiguous_output_anchor"),
        ("one two", "", "empty_output_anchor"),
    ],
)
def test_exact_output_replacement_rejects_unsafe_anchors(
    text: str,
    anchor: str,
    code: str,
) -> None:
    state = establish(
        source="source",
        output=text,
        direction=Direction.FR_TO_EN_US,
        brief="Brief.",
    )
    with pytest.raises(ExactAnchorError) as caught:
        replace_output_exact(state, output_anchor=anchor, output_replacement="replacement")
    assert caught.value.code == code
    assert state.output == text


def test_full_replacement_retains_or_replaces_explicit_state() -> None:
    state = initial_state()
    revised = replace_full(state, output="Version complète révisée.")
    assert revised.source == state.source
    assert revised.brief == state.brief
    assert revised.output == full_current(revised)
    assert revised.version == 2

    replaced = replace_full(
        revised,
        source="Entirely new source.",
        output="Source entièrement nouvelle.",
        brief="New approved brief.",
    )
    assert replaced.source_word_count == 3
    assert replaced.brief == "New approved brief."
    assert replaced.version == 3
