from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "prompts"
MODE_PROMPTS = ("translate.md", "revision.md", "internal_communications.md")


def read_prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def normalized_prompt(name: str) -> str:
    """The prompt with every whitespace run collapsed, for wrap-independent phrases."""

    return " ".join(read_prompt(name).split())


def test_prompt_directory_has_one_shared_rules_file_and_three_modes() -> None:
    assert {path.name for path in PROMPTS.glob("*.md")} == {
        "alithya_rules.md",
        "translate.md",
        "revision.md",
        "internal_communications.md",
        "protocol.md",
        "docx_protocol.md",
    }


def test_shared_terminology_exists_once_and_not_in_mode_prompts() -> None:
    shared = normalized_prompt("alithya_rules.md")
    assert "## Authorized terminology" in shared
    assert "Source term | Approved form | Direction / locale scope | Contextual note" in shared
    assert "partenaire d'affaires Capital humain (PACH)" in shared
    assert "HCBP | PACH" in shared
    assert "employés permanents" in shared
    assert "robot conversationnel" in shared
    for name in MODE_PROMPTS:
        prompt = normalized_prompt(name)
        assert "partenaire d'affaires Capital humain" not in prompt
        assert "HCBP | PACH" not in prompt


def test_shared_rules_do_not_own_protocol_or_state_mechanics() -> None:
    shared = normalized_prompt("alithya_rules.md")
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
    prompt = normalized_prompt("translate.md")
    for direction in (
        "French→US English",
        "English→Canadian",
        "France French",
        "International French",
    ):
        assert direction in prompt
    assert "Selecting this section already establishes translation intent" in prompt
    assert "bare English source" in prompt
    assert "ask only which French variety" in prompt
    assert "Clearly French source defaults to US English" in prompt
    assert "Preserve source tone and register by default" in prompt
    assert "Comm internes" not in prompt


def test_revision_reviews_existing_translations_and_redirects_new_translation() -> None:
    prompt = normalized_prompt("revision.md")
    for depth in ("proofread", "review/copyedit", "revision", "rewrite"):
        assert f"`{depth}`" in prompt
    for expected in (
        "supplies both a source-language original and its existing translation",
        "compare the entire pair and return the corrected target text",
        "source text without supplying an existing target draft",
        "use the supplied original as the authority for meaning",
        "complete supplied comparison pair",
        "complete revised target text",
    ):
        assert expected in prompt
    assert "redirect to Translate" in prompt
    assert "redirect to Internal communications" in prompt
    assert "Do not create missing translated passages" in prompt
    assert "same-language locale adaptation" in prompt
    assert "Comm internes" not in prompt


def test_revision_uses_relevant_references_as_active_style_authority() -> None:
    prompt = normalized_prompt("revision.md")
    for expected in (
        "active authority for style and terminology",
        "sentence patterns",
        "heading forms",
        "list conventions",
        "reuse the reference's wording exactly",
        "closest natural analogue",
        "never the canonical `source` or `output`",
    ):
        assert expected in prompt
    assert "Do not copy unrelated facts" in prompt


def test_translate_reuses_grounded_precedent_without_inventing_history() -> None:
    prompt = normalized_prompt("translate.md")
    for expected in (
        "active canonical translation",
        "prior conversation deliverables",
        "reuse its target wording exactly",
        "Ground every claim about earlier wording",
        "exact attested choice",
        "Never invent, guess, or imply access",
        "A `reference` attachment is precedent only",
    ):
        assert expected in prompt


def test_docx_protocol_excludes_reference_blocks_from_working_document_state() -> None:
    prompt = normalized_prompt("docx_protocol.md")
    assert "A `source` attachment" in prompt
    assert "never use their block set as the returned replacement map" in prompt
    assert "number their blocks the same way" in prompt
    assert "make their text canonical source or output" in prompt
    # The working text after the first version, and uploads before a question.
    assert "build every later change from the canonical `docx_blocks`" in prompt
    assert "most recent `source` attachment" in prompt
    assert "the only valid mutation is `establish`" in prompt
    assert "never contains a line break" in prompt


def test_internal_communications_redirects_external_editing_but_refines_own_draft() -> None:
    prompt = normalized_prompt("internal_communications.md")
    assert "`Comm internes`" in prompt
    assert "not a person" in prompt
    assert "completed prose whose primary need is proofreading" in prompt
    assert "redirect to Revision" in prompt
    assert "refine a draft that this section created in the same conversation" in prompt
    assert "If asked to translate existing text, refuse briefly" in prompt
    for protected_fact in ("dates", "policies", "links", "signatories", "departments"):
        assert protected_fact in prompt


def test_all_modes_are_restrained_professional_copilots() -> None:
    expected_examples = {
        "translate.md": ("translation copilot", "terminology choice"),
        "revision.md": ("editing copilot", "recurring weakness"),
        "internal_communications.md": ("communications copilot", "missing owner or deadline"),
    }
    for name, examples in expected_examples.items():
        prompt = normalized_prompt(name)
        assert all(example in prompt for example in examples)
        assert "Before starting," in prompt
        assert "ask a focused question only when its answer is required" in prompt
        assert "that is not a blocker must not delay the work" in prompt
        assert "Do not delay clear," in prompt
        assert "turn intake into a broad interview" in prompt
        assert "one to three high-value points" in prompt
        assert "nothing material to add" in prompt
        assert "Never manufacture commentary" in prompt
        assert "at most one concise `advice` block" in prompt


def test_locale_ownership_and_isolation_are_explicit() -> None:
    shared = normalized_prompt("alithya_rules.md")
    translate = normalized_prompt("translate.md")
    revision = normalized_prompt("revision.md")
    internal = normalized_prompt("internal_communications.md")
    assert "France French uses normal France vocabulary" in shared
    assert "Do not impose Canadian defaults" in shared
    assert "selected target locale" in translate
    assert "preserve them" in revision
    assert "selected draft locale" in internal
    for mode in (translate, revision, internal):
        assert "France" in mode and "Canadian" in mode
    assert "never changes a France or International French selection" in translate
    assert "adaptation between locales of the same language" in translate
    assert "merely because the rules are shared" in revision
    assert "merely because the rules are shared" in internal


def test_protocol_has_closed_technical_grammar_without_mode_behavior() -> None:
    protocol = normalized_prompt("protocol.md")
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
    # generation._DELTA_CHARS mirrors the upper end of this range.
    assert "roughly 200\u2013600 characters per delta" in protocol
    assert "at most 16 blocks" in protocol
    assert "Headings, tables, horizontal rules, and code fences do not render" in protocol
    assert "never from the transcript or from memory" in protocol
    assert "copy `active_canonical_work.application_state.version` exactly" in protocol
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


def test_shared_rules_distinguish_link_destinations_and_ordinary_labels() -> None:
    shared = normalized_prompt("alithya_rules.md")
    assert "A visible linked label is ordinary prose" in shared
    assert "leaving its destination exact" in shared
    for expected in (
        "| Trusted advisor | conseiller de confiance |",
        "| Digital transformation | transformation numérique |",
        "| Website | site Web |",
        "| Event | évènement |",
    ):
        assert expected in shared

    # Language-owner-disputed terminology remains unchanged.
    for protected in (
        "| AI slop | IA slop |",
        "| Bring to life | Concrétiser |",
        "| Kind regards | Cordialement |",
        "| President and Chief Executive Officer | Président et chef de la direction |",
        "| Heatmaps | Cartes de chaleur (heatmaps) |",
    ):
        assert protected in shared


def test_official_public_names_use_established_translations_without_invention() -> None:
    shared = normalized_prompt("alithya_rules.md")
    assert "For legislation, regulations, treaties, courts, government bodies" in shared
    assert "first determine whether an official target-language name is established" in shared
    assert "use that official form exactly, including its established acronym" in shared
    assert "Never create an official name or acronym by translating its components" in shared
    assert "cannot be established confidently, retain the complete source-language name" in (shared)
    assert "flag the need for verification" in shared
    assert "Act respecting labour standards" not in shared


def test_precedent_continuations_and_conversation_language_are_unambiguous() -> None:
    translate = normalized_prompt("translate.md")
    revision = normalized_prompt("revision.md")
    internal = normalized_prompt("internal_communications.md")
    # Approved terminology is mandatory; earlier wording and references are precedent.
    assert "Approved terminology and official names outrank precedent" in translate
    assert "protected content still outrank the reference" in revision
    # Pasting a new, unrelated text must not be appended to the working document.
    assert "a new self-contained text is a separate translation" in translate
    assert "a new self-contained text is a separate document" in revision
    assert "Keep the source's paragraphs, headings, and lists" in translate
    assert "Translate works only between French and English" in translate
    assert "infer the depth from the request's wording" in revision
    for prompt in (translate, revision, internal):
        assert "An explicit user choice of conversation language overrides both" in prompt
    for prompt in (revision, internal):
        assert "Never reconstruct canonical work from transcript fragments" in prompt
        assert "use the broader `full` operation" in prompt


def test_canadian_usage_rules_read_in_one_direction() -> None:
    shared = normalized_prompt("alithya_rules.md")
    assert "Prefer `défi` to `challenge`, `occasion` to `opportunité`" in shared
    canadian_forms = shared.split("Canadian French uses established Canadian forms", 1)[1]
    # `logiciel` is approved for all French locales, not a Canadian-only form.
    assert "`logiciel`" not in canadian_forms.split(".", 1)[0]


def test_owner_decisions_on_terminology_gender_and_bilingual_drafts() -> None:
    shared = normalized_prompt("alithya_rules.md")
    internal = normalized_prompt("internal_communications.md")
    # The user has the final say on wording; Oveo mentions the rule it departs from.
    assert "The user has the final say" in shared
    # Approved organizational terms and titles also apply from French to English.
    assert "For French→English, render an approved French form" in shared
    assert "(for example, `PACH` becomes `HCBP`)" in shared
    # Outside translation a user's own wording is flagged, never silently replaced.
    assert "keep it, flag it in advice, and ask before replacing it" in shared
    assert "only when the user asks for it" in shared
    assert "ask whether the feminine form applies. Never infer it from a name." in shared
    assert "(English, French, or both)" in internal
    assert "When the user asks for both English and French" in internal
    assert "French first unless the user asks otherwise" in internal


def test_business_case_and_oqlf_formats_follow_owner_decisions() -> None:
    shared = normalized_prompt("alithya_rules.md")
    translate = normalized_prompt("translate.md")
    # `étude de cas` means "case study"; a business case justifies an investment.
    assert "| Business case | analyse de rentabilisation |" in shared
    assert "| Business case | étude de cas |" not in shared
    assert "`dossier d'affaires`" in shared
    # Every French variety uses OQLF typography and formats; English uses US formats.
    assert "All French, including France and International French, follows OQLF" in shared
    assert "no space before `;`, `!`, or `?`" in shared
    assert "(`1 000,50 $`, `15 %`); times as `14 h 30`" in shared
    assert "US English uses US spelling and formats: `$1,000.50`" in shared
    assert "Shared Canadian vocabulary never changes a France" in translate
    assert "Localize ordinary written dates, times, numbers, and currency amounts" in translate
