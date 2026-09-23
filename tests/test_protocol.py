import json
import time

import pytest

from oveo.protocol import (
    AppendState,
    ContentBlock,
    DocxBlockReplacement,
    EstablishState,
    FullState,
    NoState,
    OutputReplacement,
    ProtocolDecoder,
    ProtocolError,
    ReplaceState,
    SourceOutputReplacement,
    validate_content_blocks,
    validate_stored_document,
)


def event(value: dict[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def complete_stream(state: dict[str, object], *, block_type: str = "deliverable") -> bytes:
    return b"".join(
        (
            event({"v": 1, "event": "response_start"}),
            event({"v": 1, "event": "block_start", "id": "b1", "type": block_type}),
            event({"v": 1, "event": "block_delta", "id": "b1", "text": "Visible"}),
            event({"v": 1, "event": "block_end", "id": "b1"}),
            event({"v": 1, "event": "state", **state}),
            event({"v": 1, "event": "response_end"}),
        )
    )


def decode_state(state: dict[str, object]):
    decoder = ProtocolDecoder()
    decoder.feed(complete_stream(state))
    return decoder.finish().state


def test_incremental_decoder_preserves_unicode_and_hides_typed_state() -> None:
    stream = b"".join(
        (
            event({"v": 1, "event": "response_start"}),
            event({"v": 1, "event": "block_start", "id": "b1", "type": "deliverable"}),
            event({"v": 1, "event": "block_delta", "id": "b1", "text": "Allô 🌍\n"}),
            event({"v": 1, "event": "block_delta", "id": "b1", "text": '{"v":1}'}),
            event({"v": 1, "event": "block_end", "id": "b1"}),
            event({"v": 1, "event": "block_start", "id": "b2", "type": "advice"}),
            event({"v": 1, "event": "block_delta", "id": "b2", "text": "Useful note."}),
            event({"v": 1, "event": "block_end", "id": "b2"}),
            event({"v": 1, "event": "state", "operation": "none"}),
            b'{"v":1,"event":"response_end"}',
        )
    )
    decoder = ProtocolDecoder()
    seen_text: list[str] = []
    seen_states = []
    for byte in stream:
        for decoded in decoder.feed(bytes([byte])):
            if decoded.text is not None:
                seen_text.append(decoded.text)
            if decoded.state is not None:
                seen_states.append(decoded.state)
    document = decoder.finish()

    assert seen_text == ["Allô 🌍\n", '{"v":1}', "Useful note."]
    assert seen_states == [NoState()]
    assert document.blocks == (
        ContentBlock(type="deliverable", text='Allô 🌍\n{"v":1}'),
        ContentBlock(type="advice", text="Useful note."),
    )
    assert document.state == NoState()
    assert document.to_storage() == {
        "version": 1,
        "blocks": [
            {"type": "deliverable", "text": 'Allô 🌍\n{"v":1}'},
            {"type": "advice", "text": "Useful note."},
        ],
    }


def test_decoder_returns_each_typed_state_operation() -> None:
    establish = decode_state(
        {
            "operation": "establish",
            "source": "Complete source.",
            "brief": {"direction": "en-US-fr-CA", "tone": "professional"},
        }
    )
    assert establish == EstablishState(
        source="Complete source.",
        brief={"direction": "en-US-fr-CA", "tone": "professional"},
    )

    docx_establish = decode_state(
        {
            "operation": "establish",
            "brief": {"direction": "en-fr"},
            "docx_blocks": [
                {
                    "id": "p000001",
                    "text": "Sortie {{OVEO_LINK_l000001}}lien{{/OVEO_LINK_l000001}}",
                }
            ],
        }
    )
    assert docx_establish == EstablishState(
        brief={"direction": "en-fr"},
        docx_blocks=(
            DocxBlockReplacement(
                id="p000001",
                text="Sortie {{OVEO_LINK_l000001}}lien{{/OVEO_LINK_l000001}}",
            ),
        ),
    )
    append = decode_state(
        {
            "operation": "append",
            "base_version": 2,
            "source_addition": "New source.",
            "output_addition": "Nouvelle source.",
            "source_separator": "line",
            "output_separator": "paragraph",
            "brief": {"direction": "en-US-fr-FR"},
        }
    )
    assert append == AppendState(
        base_version=2,
        source_addition="New source.",
        output_addition="Nouvelle source.",
        source_separator="line",
        output_separator="paragraph",
        brief={"direction": "en-US-fr-FR"},
    )

    replace = decode_state(
        {
            "operation": "replace",
            "base_version": 3,
            "replacements": [
                {
                    "source_anchor": "old source",
                    "source_replacement": "new source",
                    "output_anchor": "ancienne sortie",
                    "output_replacement": "nouvelle sortie",
                },
                {
                    "output_anchor": "remove this",
                    "output_replacement": "",
                },
            ],
            "brief": {"audience": "employees"},
        }
    )
    assert replace == ReplaceState(
        base_version=3,
        replacements=(
            SourceOutputReplacement(
                source_anchor="old source",
                source_replacement="new source",
                output_anchor="ancienne sortie",
                output_replacement="nouvelle sortie",
            ),
            OutputReplacement(
                output_anchor="remove this",
                output_replacement="",
            ),
        ),
        brief={"audience": "employees"},
    )

    full = decode_state(
        {
            "operation": "full",
            "base_version": 4,
            "source": "Complete revised source.",
            "brief": {"audience": "employees"},
        }
    )
    assert full == FullState(
        base_version=4,
        source="Complete revised source.",
        brief={"audience": "employees"},
    )


def test_docx_state_requires_consecutive_bounded_block_ids() -> None:
    state = {
        "operation": "full",
        "base_version": 1,
        "docx_blocks": [{"id": "p000002", "text": "Updated"}],
    }
    with pytest.raises(ProtocolError, match="invalid_docx_blocks"):
        ProtocolDecoder().feed(complete_stream(state))
    with pytest.raises(ProtocolError, match="invalid_docx_blocks"):
        ProtocolDecoder(max_docx_blocks=1).feed(
            complete_stream(
                {
                    **state,
                    "docx_blocks": [
                        {"id": "p000001", "text": "One"},
                        {"id": "p000002", "text": "Two"},
                    ],
                }
            )
        )


@pytest.mark.parametrize(
    ("lines", "code"),
    [
        (
            [{"v": 1, "event": "block_start", "id": "b1", "type": "conversation"}],
            "missing_response_start",
        ),
        (
            [{"v": 1, "event": "response_start"}, {"v": 1, "event": "response_end"}],
            "missing_state",
        ),
        (
            [
                {"v": 1, "event": "response_start"},
                {"v": 1, "event": "block_start", "id": "b2", "type": "conversation"},
            ],
            "invalid_block_id",
        ),
        (
            [
                {"v": 1, "event": "response_start"},
                {"v": 1, "event": "state", "operation": "none"},
            ],
            "invalid_block_count",
        ),
    ],
)
def test_decoder_rejects_invalid_event_order(lines: list[dict[str, object]], code: str) -> None:
    decoder = ProtocolDecoder()
    with pytest.raises(ProtocolError) as caught:
        for line in lines:
            decoder.feed(event(line))
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("tail", "code"),
    [
        ([{"v": 1, "event": "response_end"}], "missing_state"),
        (
            [
                {"v": 1, "event": "state", "operation": "none"},
                {"v": 1, "event": "state", "operation": "none"},
            ],
            "duplicate_state",
        ),
        (
            [
                {"v": 1, "event": "state", "operation": "none"},
                {"v": 1, "event": "block_start", "id": "b2", "type": "advice"},
            ],
            "event_after_state",
        ),
    ],
)
def test_state_is_required_exactly_once_immediately_before_end(
    tail: list[dict[str, object]], code: str
) -> None:
    decoder = ProtocolDecoder()
    prefix = [
        {"v": 1, "event": "response_start"},
        {"v": 1, "event": "block_start", "id": "b1", "type": "conversation"},
        {"v": 1, "event": "block_delta", "id": "b1", "text": "Question?"},
        {"v": 1, "event": "block_end", "id": "b1"},
    ]
    with pytest.raises(ProtocolError) as caught:
        for item in (*prefix, *tail):
            decoder.feed(event(item))
    assert caught.value.code == code


def test_conversation_blocks_can_only_have_none_state() -> None:
    decoder = ProtocolDecoder()
    with pytest.raises(ProtocolError) as caught:
        decoder.feed(
            complete_stream(
                {
                    "operation": "establish",
                    "source": "Source",
                    "brief": {"purpose": "test"},
                },
                block_type="conversation",
            )
        )
    assert caught.value.code == "invalid_state_for_conversation"


@pytest.mark.parametrize(
    "state",
    [
        {"operation": "none", "extra": True},
        {
            "operation": "append",
            "base_version": 1,
            "source_addition": "source",
            "output_addition": "output",
            "source_separator": "paragraph",
            "output_separator": "paragraph",
            "extra": "forbidden",
        },
        {
            "operation": "full",
            "base_version": 1,
            "source": None,
        },
        {
            "operation": "replace",
            "base_version": 1,
            "replacements": [
                {
                    "output_anchor": "old",
                    "output_replacement": "new",
                    "extra": "forbidden",
                }
            ],
        },
    ],
)
def test_state_rejects_extra_keys_and_invalid_optional_values(
    state: dict[str, object],
) -> None:
    decoder = ProtocolDecoder()
    with pytest.raises(ProtocolError):
        decoder.feed(complete_stream(state))


def test_state_bounds_text_brief_replacements_and_duplicate_anchors() -> None:
    with pytest.raises(ProtocolError, match="state_too_large"):
        ProtocolDecoder(max_state_text_chars=5).feed(
            complete_stream(
                {
                    "operation": "append",
                    "base_version": 1,
                    "source_addition": "123",
                    "output_addition": "456",
                    "source_separator": "none",
                    "output_separator": "none",
                }
            )
        )

    with pytest.raises(ProtocolError, match="state_brief_too_large"):
        ProtocolDecoder(max_brief_bytes=4).feed(
            complete_stream(
                {
                    "operation": "establish",
                    "source": "source",
                    "brief": {"purpose": "test"},
                }
            )
        )

    with pytest.raises(ProtocolError, match="invalid_replacement_count"):
        ProtocolDecoder(max_replacements=1).feed(
            complete_stream(
                {
                    "operation": "replace",
                    "base_version": 1,
                    "replacements": [
                        {"output_anchor": "one", "output_replacement": "1"},
                        {"output_anchor": "two", "output_replacement": "2"},
                    ],
                }
            )
        )

    with pytest.raises(ProtocolError, match="duplicate_replacement_anchor"):
        ProtocolDecoder().feed(
            complete_stream(
                {
                    "operation": "replace",
                    "base_version": 1,
                    "replacements": [
                        {"output_anchor": "same", "output_replacement": "1"},
                        {"output_anchor": "same", "output_replacement": "2"},
                    ],
                }
            )
        )


def test_decoder_rejects_duplicate_keys_without_echoing_content() -> None:
    decoder = ProtocolDecoder()
    decoder.feed(b'{"v":1,"event":"response_start"}\n')
    private_marker = "private-source-marker"
    with pytest.raises(ProtocolError) as caught:
        decoder.feed(
            (
                '{"v":1,"event":"block_start","id":"b1","id":"'
                + private_marker
                + '","type":"conversation"}\n'
            ).encode()
        )
    assert caught.value.code == "duplicate_json_key"
    assert private_marker not in str(caught.value)


def test_decoder_rejects_invalid_utf8_and_data_after_terminal_event() -> None:
    with pytest.raises(ProtocolError, match="invalid_utf8"):
        ProtocolDecoder().feed(b"\xff")

    decoder = ProtocolDecoder()
    decoder.feed(complete_stream({"operation": "none"}, block_type="conversation"))
    with pytest.raises(ProtocolError, match="data_after_response_end"):
        decoder.feed(b"x")


def test_typed_block_and_stored_document_validation_excludes_hidden_state() -> None:
    blocks = validate_content_blocks(
        [
            {"type": "deliverable", "text": "Option one"},
            {"type": "deliverable", "text": "Option two"},
            {"type": "advice", "text": "Choose for tone."},
        ]
    )
    document = validate_stored_document(
        {"version": 1, "blocks": [{"type": block.type, "text": block.text} for block in blocks]}
    )
    assert document.blocks == blocks
    assert document.state == NoState()
    assert "state" not in document.to_storage()

    with pytest.raises(ProtocolError, match="invalid_block_order"):
        validate_content_blocks(
            [
                {"type": "conversation", "text": "Hello"},
                {"type": "advice", "text": "No"},
            ]
        )


def test_completed_blocks_reject_whitespace_but_deltas_may_be_whitespace() -> None:
    decoder = ProtocolDecoder()
    decoder.feed(
        b"".join(
            (
                event({"v": 1, "event": "response_start"}),
                event({"v": 1, "event": "block_start", "id": "b1", "type": "conversation"}),
                event({"v": 1, "event": "block_delta", "id": "b1", "text": "  \n"}),
                event({"v": 1, "event": "block_delta", "id": "b1", "text": "Visible"}),
                event({"v": 1, "event": "block_end", "id": "b1"}),
                event({"v": 1, "event": "state", "operation": "none"}),
                event({"v": 1, "event": "response_end"}),
            )
        )
    )
    assert decoder.finish().blocks[0].text == "  \nVisible"

    whitespace = ProtocolDecoder()
    with pytest.raises(ProtocolError, match="empty_block"):
        whitespace.feed(
            b"".join(
                (
                    event({"v": 1, "event": "response_start"}),
                    event({"v": 1, "event": "block_start", "id": "b1", "type": "conversation"}),
                    event({"v": 1, "event": "block_delta", "id": "b1", "text": " \n\t"}),
                    event({"v": 1, "event": "block_end", "id": "b1"}),
                )
            )
        )

    with pytest.raises(ProtocolError, match="empty_block"):
        validate_content_blocks([{"type": "deliverable", "text": " \n\t"}])


@pytest.mark.parametrize("separator", ["", "two-lines", 1, None])
def test_append_rejects_unknown_or_missing_separator(separator: object) -> None:
    state: dict[str, object] = {
        "operation": "append",
        "base_version": 1,
        "source_addition": "source",
        "output_addition": "output",
        "source_separator": separator,
        "output_separator": "line",
    }
    with pytest.raises(ProtocolError):
        ProtocolDecoder().feed(complete_stream(state))


@pytest.mark.parametrize(
    "state",
    [
        {"operation": "establish", "source": "s", "output": "Visible", "brief": {"a": "b"}},
        {"operation": "full", "base_version": 1, "output": "Visible"},
    ],
)
def test_state_never_repeats_the_deliverable_as_output(state: dict[str, object]) -> None:
    with pytest.raises(ProtocolError) as caught:
        ProtocolDecoder().feed(complete_stream(state))
    assert caught.value.code == "invalid_event_fields"


def test_optional_fields_are_derived_by_the_application() -> None:
    assert decode_state({"operation": "establish", "brief": {"scope": "docx"}}) == (
        EstablishState(brief={"scope": "docx"})
    )
    assert decode_state({"operation": "full", "base_version": 2}) == FullState(base_version=2)
    assert decode_state(
        {
            "operation": "append",
            "base_version": 1,
            "source_addition": "More",
            "source_separator": "space",
            "output_separator": "space",
        }
    ) == AppendState(
        base_version=1,
        source_addition="More",
        source_separator="space",
        output_separator="space",
    )


def test_decoder_is_byte_split_invariant_and_linear_in_line_length() -> None:
    text = "é" * 600_000 + "🌍"
    stream = b"".join(
        (
            event({"v": 1, "event": "response_start"}),
            event({"v": 1, "event": "block_start", "id": "b1", "type": "deliverable"}),
            event({"v": 1, "event": "block_delta", "id": "b1", "text": text}),
            event({"v": 1, "event": "block_end", "id": "b1"}),
            event({"v": 1, "event": "state", "operation": "none"}),
            event({"v": 1, "event": "response_end"}),
        )
    )
    whole = ProtocolDecoder()
    whole.feed(stream)
    expected = whole.finish()

    split = ProtocolDecoder()
    started = time.perf_counter()
    for start in range(0, len(stream), 16):
        split.feed(stream[start : start + 16])
    elapsed = time.perf_counter() - started
    assert split.finish() == expected
    # The former re-concatenating buffer needed seconds for a line this long (audit
    # probe p09: 600,000 characters took 5.6 s); the pieces list keeps it linear.
    assert elapsed < 2.0
