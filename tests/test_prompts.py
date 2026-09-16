from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "prompts"


def read_prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def test_prompts_define_source_data_as_untrusted() -> None:
    for name in ("translate.md", "alithyagpt.md", "protocol.md"):
        text = read_prompt(name).lower()
        assert "untrusted" in text
        assert "provider routing" in text


def test_translate_prompt_contains_the_supported_directions_and_terminology() -> None:
    prompt = read_prompt("translate.md")
    for direction in (
        "French to US English",
        "English to Canadian French",
        "English to France French",
        "English to International French",
    ):
        assert direction in prompt
    assert "partenaire d'affaires Capital humain" in prompt
    assert "employés permanents" in prompt
    assert "robot conversationnel" in prompt


def test_alithyagpt_restores_translation_task_without_placeholder_profiles() -> None:
    prompt = read_prompt("alithyagpt.md")
    assert "## Task 3: Translation" in prompt
    assert "Faithfulness does not require literal syntax" in prompt
    assert "Comm internes" in prompt
    assert "[to be completed]" not in prompt


def test_protocol_has_a_closed_versioned_event_grammar() -> None:
    protocol = read_prompt("protocol.md")
    for event in (
        "response_start",
        "block_start",
        "block_delta",
        "block_end",
        '"event":"state"',
        "response_end",
    ):
        assert event in protocol
    for block_type in ("conversation", "deliverable", "advice"):
        assert f"`{block_type}`" in protocol
    assert '"version":1' in protocol
    for operation in ("none", "establish", "append", "replace", "full"):
        assert f'"operation":"{operation}"' in protocol
    assert "20 to 200 characters" in protocol
    assert "immediately before `response_end`" in protocol


def test_brand_logo_keeps_the_verified_oveo_semantics() -> None:
    logo = (ROOT / "frontend" / "src" / "BrandLogo.tsx").read_text(encoding="utf-8")
    assert 'aria-label={decorative ? undefined : "Oveo"}' in logo
    assert 'className="brand-mark"' in logo
    assert 'className="brand-wordmark"' in logo
    assert ">OVEO<" in logo
