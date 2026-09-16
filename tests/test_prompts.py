from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "prompts"
MODE_PROMPTS = ("translate.md", "revision.md", "internal_communications.md")


def read_prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def test_prompt_directory_has_one_shared_rules_file_and_three_modes() -> None:
    assert {path.name for path in PROMPTS.glob("*.md")} == {
        "alithya_rules.md",
        "translate.md",
        "revision.md",
        "internal_communications.md",
        "protocol.md",
    }


def test_shared_terminology_exists_once_and_not_in_mode_prompts() -> None:
    shared = read_prompt("alithya_rules.md")
    assert "## Authorized terminology" in shared
    assert "Source term | Approved form | Direction / locale scope | Contextual note" in shared
    assert "partenaire d'affaires Capital humain (PACH)" in shared
    assert "HCBP | PACH" in shared
    assert "employés permanents" in shared
    assert "robot conversationnel" in shared
    for name in MODE_PROMPTS:
        prompt = read_prompt(name)
        assert "partenaire d'affaires Capital humain" not in prompt
        assert "HCBP | PACH" not in prompt


def test_shared_rules_do_not_own_protocol_or_state_mechanics() -> None:
    shared = read_prompt("alithya_rules.md")
    for forbidden in (
        "response_start",
        "block_start",
        "block_delta",
        '"event":"state"',
        '"operation":"establish"',
        "base_version",
        "source_anchor",
    ):
        assert forbidden not in shared


def test_translate_intake_is_narrow_and_does_not_inherit_internal_voice() -> None:
    prompt = read_prompt("translate.md")
    for direction in (
        "French→US English",
        "English→Canadian",
        "France French",
        "International French",
    ):
        assert direction in prompt
    assert "Selecting this section already\nestablishes translation intent" in prompt
    assert "bare English source" in prompt
    assert "ask only which French\n  variety" in prompt
    assert "Clearly French source defaults to US English" in prompt
    assert "Preserve source tone and register by default" in prompt
    assert "Comm internes" not in prompt


def test_revision_redirects_translation_and_new_internal_drafting() -> None:
    prompt = read_prompt("revision.md")
    for depth in ("proofread", "review/copyedit", "revision", "rewrite"):
        assert f"`{depth}`" in prompt
    assert "asks to translate, refuse briefly and redirect to Translate" in prompt
    assert "redirect to Internal communications" in prompt
    assert "Do not translate" in prompt
    assert "same-language locale adaptation" in prompt
    assert "Comm internes" not in prompt


def test_internal_communications_redirects_external_editing_but_refines_own_draft() -> None:
    prompt = read_prompt("internal_communications.md")
    assert "`Comm internes`" in prompt
    assert "not a person" in prompt
    assert "completed prose whose primary need is proofreading" in prompt
    assert "redirect to Revision" in prompt
    assert "refine a draft that this section created in the same conversation" in prompt
    assert "If asked to translate existing text, refuse briefly" in prompt
    for protected_fact in ("dates", "policies", "links", "signatories", "departments"):
        assert protected_fact in prompt


def test_locale_ownership_and_isolation_are_explicit() -> None:
    shared = read_prompt("alithya_rules.md")
    translate = read_prompt("translate.md")
    revision = read_prompt("revision.md")
    internal = read_prompt("internal_communications.md")
    assert "France French uses normal France vocabulary" in shared
    assert "Do not impose Canadian defaults" in shared
    assert "selected target locale" in translate
    assert "preserve them" in revision
    assert "selected draft locale" in internal
    for mode in (translate, revision, internal):
        assert "France" in mode and "Canadian" in mode
    assert "never changes\na France or International French selection" in translate
    assert "merely because the rules are shared" in revision
    assert "merely because the rules are shared" in internal


def test_protocol_has_closed_technical_grammar_without_mode_behavior() -> None:
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
    for operation in ("none", "establish", "append", "replace", "full"):
        assert f'"operation":"{operation}"' in protocol
    assert "roughly 20" in protocol and "200 characters" in protocol
    assert "mode prompt decides response meaning" in protocol
    for forbidden in (
        "French→US English",
        "Canadian French",
        "Comm internes",
        "proofread",
        "employee-facing",
    ):
        assert forbidden not in protocol


def test_brand_logo_keeps_the_verified_oveo_semantics() -> None:
    logo = (ROOT / "frontend" / "src" / "BrandLogo.tsx").read_text(encoding="utf-8")
    assert 'aria-label={decorative ? undefined : "Oveo"}' in logo
    assert 'className="brand-mark"' in logo
    assert 'className="brand-wordmark"' in logo
    assert ">OVEO<" in logo
