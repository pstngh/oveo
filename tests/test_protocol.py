import json

import pytest

from oveo.protocol import (
    AppendState,
    ContentBlock,
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
            "output": "Source complète.",
            "brief": {"direction": "en-US-fr-CA", "tone": "professional"},
        }
    )
    assert establish == EstablishState(
        source="Complete source.",
        output="Source complète.",
        brief={"direction": "en-US-fr-CA", "tone": "professional"},
    )

    append = decode_state(
        {
            "operation": "append",
            "base_version": 2,
            "source_addition": "New source.",
            "output_addition": "Nouvelle source.",
        }
    )
    assert append == AppendState(
        base_version=2,
        source_addition="New source.",
        output_addition="Nouvelle source.",
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
    )

    full = decode_state(
        {
            "operation": "full",
            "base_version": 4,
            "output": "Complete replacement.",
            "source": "Complete revised source.",
            "brief": {"audience": "employees"},
        }
    )
    assert full == FullState(
        base_version=4,
        output="Complete replacement.",
        source="Complete revised source.",
        brief={"audience": "employees"},
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
                    "output": "Output",
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
            "extra": "forbidden",
        },
        {
            "operation": "full",
            "base_version": 1,
            "output": "output",
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
                }
            )
        )

    with pytest.raises(ProtocolError, match="state_brief_too_large"):
        ProtocolDecoder(max_brief_bytes=4).feed(
            complete_stream(
                {
                    "operation": "establish",
                    "source": "source",
                    "output": "output",
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
