"""Prompt evaluation scenarios that run through the real generation path.

Each scenario drives `GenerationManager` end to end: prompt composition, protocol
decoding, strict retries, canonical state, and Word export. `test_live_evals.py`
runs every scenario offline against a known-good answer (proving the harness and
its checks) and against a known-bad answer (proving the checks can fail). With
`OVEO_LIVE_EVALS=1` and an OpenRouter key it runs them against the pinned model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from oveo.attachments import ValidatedAttachment, count_words
from oveo.config import Settings
from oveo.context import AttachmentRole, Mode, build_provider_messages
from oveo.db import Database
from oveo.docx import extract_docx, render_docx
from oveo.generation import (
    GenerationManager,
    GenerationProvider,
    ProviderCompletion,
    ProviderRequest,
    _parse_context_summary,
)
from oveo.models import Message, Thread, User, WorkItem, WorkVersion
from tests.docx_fixtures import R, W, make_docx

NBSP = chr(0x00A0)
_FINISHED = frozenset({"completed", "failed", "stopped"})
_FRENCH_WORDS = frozenset({"de", "des", "du", "la", "le", "les", "et", "pour", "une", "un"})


@dataclass(frozen=True)
class Turn:
    text: str
    docx: bytes | None = None
    role: AttachmentRole = "source"


@dataclass
class Outcome:
    """What a scenario left behind: one snapshot per turn and the saved work."""

    snapshots: list[dict[str, Any]]
    latest: WorkVersion | None
    work_items: int
    title: str | None

    def blocks(self, kind: str, turn: int = -1) -> list[str]:
        return [
            str(block["text"]) for block in self.snapshots[turn]["blocks"] if block["type"] == kind
        ]

    def deliverable(self, turn: int = -1) -> str:
        return "\n\n".join(self.blocks("deliverable", turn))

    def notes(self, turn: int = -1) -> str:
        return "\n\n".join(self.blocks("conversation", turn) + self.blocks("advice", turn))


def expect(condition: bool, message: str, outcome: Outcome | None = None) -> None:
    if not condition:
        detail = ""
        if outcome is not None:
            detail = "\n" + json.dumps(outcome.snapshots, ensure_ascii=False, indent=1)[:4000]
        raise AssertionError(message + detail)


def completed(outcome: Outcome) -> None:
    for index, snapshot in enumerate(outcome.snapshots):
        expect(
            snapshot["status"] == "completed",
            f"turn {index + 1} ended {snapshot['status']} ({snapshot.get('error_code')})",
            outcome,
        )


@dataclass(frozen=True)
class Scenario:
    name: str
    mode: Mode
    turns: tuple[Turn, ...]
    check: Callable[[Outcome], None]
    # A known-good model answer for each turn, and one whose last answer must fail.
    good: tuple[bytes, ...]
    bad: tuple[bytes, ...]
    title: str = "Evaluation Conversation"
    wait_for_title: bool = False


def ndjson(blocks: Sequence[tuple[str, str]], state: dict[str, object]) -> bytes:
    events: list[dict[str, object]] = [{"v": 1, "event": "response_start"}]
    for index, (kind, text) in enumerate(blocks, start=1):
        block_id = f"b{index}"
        events += [
            {"v": 1, "event": "block_start", "id": block_id, "type": kind},
            {"v": 1, "event": "block_delta", "id": block_id, "text": text},
            {"v": 1, "event": "block_end", "id": block_id},
        ]
    events += [{"v": 1, "event": "state", **state}, {"v": 1, "event": "response_end"}]
    return ("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n").encode()


def _establish(text: str, source: str, *, advice: str | None = None) -> bytes:
    blocks = [("deliverable", text)] + ([("advice", advice)] if advice else [])
    return ndjson(blocks, {"operation": "establish", "source": source, "brief": {"eval": True}})


def _answer(text: str, kind: str = "conversation") -> bytes:
    return ndjson([(kind, text)], {"operation": "none"})


class CannedProvider:
    """Answers each chat call with the next scripted answer."""

    def __init__(self, answers: Sequence[bytes], *, title: str, summary: bytes = b"") -> None:
        self.answers = list(answers)
        self.title = title
        self.summary = summary

    async def generate(
        self, request: ProviderRequest, emit: Any, cancel_event: asyncio.Event
    ) -> ProviderCompletion:
        del cancel_event
        if request.purpose == "title":
            await emit(self.title.encode())
        elif request.purpose == "summary":
            await emit(self.summary)
        else:
            await emit(self.answers.pop(0))
        return ProviderCompletion(cost_microusd=1)


def _attachment(content: bytes) -> ValidatedAttachment:
    extracted = extract_docx(content)
    return ValidatedAttachment(
        original_name="evaluation.docx",
        content=content,
        byte_count=len(content),
        word_count=count_words(extracted.plain_text),
        sha256=hashlib.sha256(content).hexdigest(),
        document_blocks=extracted.blocks,
        plain_text=extracted.plain_text,
    )


async def run_scenario(
    scenario: Scenario,
    *,
    database: Database,
    settings: Settings,
    user: User,
    provider: GenerationProvider,
    wait_seconds: float,
) -> Outcome:
    manager = GenerationManager(database, settings, provider)
    thread_id: str | None = None
    snapshots: list[dict[str, Any]] = []
    try:
        for index, turn in enumerate(scenario.turns):
            submission = await manager.submit_turn(
                requester_id=user.id,
                client_request_id=f"{scenario.name}-{index}",
                text=turn.text,
                attachment=_attachment(turn.docx) if turn.docx else None,
                attachment_role=turn.role,
                thread_id=thread_id,
                owner_id=None if thread_id else user.id,
                mode=None if thread_id else scenario.mode,
            )
            thread_id = submission.thread_id
            snapshots.append(await _finished(manager, submission.generation_id, wait_seconds))
        assert thread_id is not None
        title = await _title(database, thread_id, wait_seconds if scenario.wait_for_title else 0.0)
        async with database.sessions() as db:
            latest = await db.scalar(
                select(WorkVersion)
                .join(WorkItem, WorkVersion.work_item_id == WorkItem.id)
                .where(WorkItem.thread_id == thread_id, WorkItem.active.is_(True))
                .order_by(WorkVersion.version_no.desc())
                .limit(1)
            )
            work_items = int(
                await db.scalar(
                    select(func.count())
                    .select_from(WorkItem)
                    .where(WorkItem.thread_id == thread_id)
                )
                or 0
            )
    finally:
        await manager.shutdown()
    return Outcome(snapshots=snapshots, latest=latest, work_items=work_items, title=title)


async def _finished(
    manager: GenerationManager, generation_id: str, wait_seconds: float
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        snapshot = await manager.get_snapshot(generation_id)
        assert snapshot is not None
        if snapshot["status"] in _FINISHED:
            return snapshot
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"generation still {snapshot['status']} after {wait_seconds}s")
        await asyncio.sleep(0.05)


async def _title(database: Database, thread_id: str, wait_seconds: float) -> str | None:
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        async with database.sessions() as db:
            thread = await db.get(Thread, thread_id)
            title = thread.title if thread is not None else None
        if title != "New conversation" or asyncio.get_running_loop().time() > deadline:
            return title
        await asyncio.sleep(0.05)


# --- Word fixtures ------------------------------------------------------------------


def _document(paragraphs: str) -> bytes:
    return make_docx(
        document_xml=(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{paragraphs}<w:sectPr/>'
            "</w:body></w:document>"
        )
    )


# `make_docx()` holds "Hello <site>." and a table cell "Cell text".
_GREETING_DOCX = make_docx()


def _link(text: str) -> str:
    return "{{OVEO_LINK_l000001}}" + text + "{{/OVEO_LINK_l000001}}"


_LAYOUT_DOCX = _document(
    "<w:p><w:r><w:t>Welcome to the team.</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Our office:</w:t><w:br/><w:t>123 Main Street</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Manager:</w:t><w:tab/><w:t>Jordan Lee</w:t></w:r></w:p>"
)
_REFERENCE_DOCX = _document(
    "<w:p><w:r><w:t>Notre agent conversationnel vous guide pas à pas.</w:t></w:r></w:p>"
)


def _docx_establish(texts: Sequence[str]) -> bytes:
    visible = "\n\n".join(
        text.replace("{{OVEO_LINK_l000001}}", "").replace("{{/OVEO_LINK_l000001}}", "")
        for text in texts
    )
    blocks = [{"id": f"p{index:06d}", "text": text} for index, text in enumerate(texts, start=1)]
    return ndjson(
        [("deliverable", visible)],
        {"operation": "establish", "brief": {"eval": True}, "docx_blocks": blocks},
    )


# --- Checks -------------------------------------------------------------------------


def _check_docx_after_question(outcome: Outcome) -> None:
    completed(outcome)
    first = [block["type"] for block in outcome.snapshots[0]["blocks"]]
    expect(first == ["conversation"], f"turn 1 should only ask a question: {first}", outcome)
    latest = outcome.latest
    expect(latest is not None and latest.docx_blocks is not None, "no Word work saved", outcome)
    assert latest is not None
    expect(latest.output_text != latest.source_text, "the Word output was not translated", outcome)


def _check_local_edit(outcome: Outcome) -> None:
    completed(outcome)
    latest = outcome.latest
    expect(latest is not None and latest.version_no == 2, "the edit was not saved", outcome)
    assert latest is not None
    expect("rencontre" in latest.output_text, "the edit is missing", outcome)
    expect("réunion" not in latest.output_text, "the old word remains", outcome)
    expect(re.search(r"14\s?h\s?30", latest.output_text) is not None, "time lost", outcome)


def _check_many_subject_lines(outcome: Outcome) -> None:
    completed(outcome)
    lines = [line for line in outcome.deliverable().splitlines() if line.strip()]
    expect(len(lines) >= 20, f"only {len(lines)} subject lines", outcome)


def _check_bilingual_review(outcome: Outcome) -> None:
    completed(outcome)
    deliverables = outcome.blocks("deliverable")
    expect(len(deliverables) == 1, "expected one corrected translation", outcome)
    text = deliverables[0].casefold()
    expect("report" in text, "the mistranslation was not corrected", outcome)
    expect("annulée" not in text, "the mistranslation remains", outcome)
    expect("postponed" not in text, "the English original was repeated", outcome)


def _check_reference_keeps_approved_term(outcome: Outcome) -> None:
    completed(outcome)
    text = outcome.deliverable().casefold()
    expect("robot conversationnel" in text, "the approved term was dropped", outcome)
    expect("agent conversationnel" not in text, "the reference's term was copied", outcome)


def _check_separate_translation(outcome: Outcome) -> None:
    completed(outcome)
    latest = outcome.latest
    expect(outcome.work_items == 2, "the new text was appended to the first", outcome)
    expect(latest is not None and latest.operation == "establish", "not a new work item", outcome)
    assert latest is not None
    expect("office" not in latest.source_text.casefold(), "the texts were merged", outcome)


def _check_no_tables(outcome: Outcome) -> None:
    completed(outcome)
    for text in outcome.blocks("conversation") + outcome.blocks("advice"):
        expect(re.search(r"^\s*\|", text, re.M) is None, "a Markdown table was used", outcome)


def _check_pach_and_english_title(outcome: Outcome) -> None:
    completed(outcome)
    text = outcome.deliverable()
    expect("HCBP" in text, "PACH was not rendered as HCBP", outcome)
    expect(
        "Human Capital Business Partner" in text, "the approved English term is missing", outcome
    )
    title = outcome.title or ""
    words = re.findall(r"[\w']+", title.casefold())
    expect(title != "New conversation" and 2 <= len(words) <= 6, f"bad title {title!r}", outcome)
    expect(not _FRENCH_WORDS.intersection(words), f"title is not English: {title!r}", outcome)


def _check_oqlf_formats(outcome: Outcome) -> None:
    completed(outcome)
    text = outcome.deliverable()
    expect(re.search(r"14\s?h\s?30", text) is not None, "time not written 14 h 30", outcome)
    expect(re.search(r"1\s000,50\s\$", text) is not None, "amount not written 1 000,50 $", outcome)
    expect(re.search(r"\s[?!;]", text) is None, "space before ? ! or ;", outcome)
    expect(re.search(r"[^\W\d_]:", text) is None, "no space before a colon", outcome)


def _check_feminine_title_is_asked(outcome: Outcome) -> None:
    completed(outcome)
    text = outcome.deliverable().casefold()
    expect("président et chef de la direction" in text, "default title not kept", outcome)
    notes = outcome.notes().casefold()
    asked = any(word in notes for word in ("féminin", "feminine", "présidente"))
    expect(asked, "the feminine form was not raised", outcome)


def _check_user_keeps_chatbot(outcome: Outcome) -> None:
    completed(outcome)
    expect("chatbot" in outcome.deliverable().casefold(), "the user's word was replaced", outcome)


def _check_bilingual_draft(outcome: Outcome) -> None:
    completed(outcome)
    deliverables = outcome.blocks("deliverable")
    expect(len(deliverables) == 1, "both versions belong in one deliverable", outcome)
    text = deliverables[0]
    expect(re.search(r"^\s*-{3,}\s*$", text, re.M) is not None, "no divider line", outcome)
    lowered = text.casefold()
    expect("bureau" in lowered and "office" in lowered, "a language is missing", outcome)
    expect(lowered.index("bureau") < lowered.index("office"), "French is not first", outcome)
    expect(text.count("aide@example.com") >= 2, "a version lost the email address", outcome)


def _check_word_layout(outcome: Outcome) -> None:
    completed(outcome)
    latest = outcome.latest
    expect(latest is not None and latest.docx_blocks is not None, "no Word work saved", outcome)
    assert latest is not None and latest.docx_blocks is not None
    texts = [block["text"] for block in latest.docx_blocks]
    expect(len(texts) == 3 and "\n" in texts[1], "the line break was lost", outcome)
    expect("\t" in texts[2], "the tab was lost", outcome)
    exported = extract_docx(render_docx(_LAYOUT_DOCX, latest.docx_blocks)).blocks
    expect([block.text for block in exported] == texts, "the export differs", outcome)


_FUTURE_EVENT = re.compile(
    r"\bwill (?:take place|be held|run|ride)\b|\blooking ahead\b|\bmark your calendars?\b"
    r"|\bconsider joining\b|\bregister\b|\bsign up\b|\bbook (?:a|your)\b",
    re.IGNORECASE,
)


def _check_past_event_is_a_recap(outcome: Outcome) -> None:
    completed(outcome)
    text = outcome.deliverable()
    expect("38,000" in text, "the amount raised is missing", outcome)
    found = _FUTURE_EVENT.search(text)
    expect(found is None, f"the past event reads as upcoming: {found}", outcome)


def _check_correction_is_applied(outcome: Outcome) -> None:
    completed(outcome)
    text = outcome.deliverable()
    expect("42" in text, "the correction's new fact is missing", outcome)
    found = _FUTURE_EVENT.search(text)
    expect(found is None, f"the draft still announces the event: {found}", outcome)


def _check_user_wording_is_merged(outcome: Outcome) -> None:
    completed(outcome)
    text = outcome.deliverable()
    expect("see you next year" in text.casefold(), "the user's wording was dropped", outcome)
    thanking = [part for part in re.split(r"\n\s*\n", text) if "thank" in part.casefold()]
    expect(len(thanking) == 1, f"{len(thanking)} paragraphs thank people", outcome)


def _check_side_text_keeps_word_work(outcome: Outcome) -> None:
    completed(outcome)
    latest = outcome.latest
    expect(latest is not None and latest.docx_blocks is not None, "Word work replaced", outcome)
    expect(outcome.work_items == 1, "the side text became the working document", outcome)
    expect(bool(outcome.deliverable(-1)), "the side text was not translated", outcome)


_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _spoken(day: date) -> str:
    return f"{_MONTHS[day.month - 1]} {day.day}, {day.year}"


# The date scenarios need an event that is really past when the model runs, so they
# use the latest finished Friday-to-Sunday weekend in the users' default time zone.
_TODAY = datetime.now(ZoneInfo("America/Toronto")).date()
_RIDE_END = _TODAY - timedelta(days=(_TODAY.weekday() - 6) % 7 or 7)
_RIDE = f"{_spoken(_RIDE_END - timedelta(days=2))} to {_spoken(_RIDE_END)}"
_RIDE_NOTES = (
    f"Make-A-Wish 48-Hour Ride, 18th edition, {_RIDE}, Aerocity of Mirabel. Alithya team: "
    "19 cyclists, 11th consecutive year, captains Sébastien Trudel and Kathryn Potvin. "
    "Over $38,000 raised."
)
_RIDE_BODY = (
    f"From {_RIDE}, 19 Alithya colleagues rode in the 18th Make-A-Wish® 48-Hour Ride at "
    "Aerocity of Mirabel, led by team captains Sébastien Trudel and Kathryn Potvin. It was "
    "our 11th consecutive year in the event, and together the team raised over $38,000 to "
    "help make wishes come true for children facing serious illnesses."
)
_RIDE_THANKS = (
    "Thank you to everyone who rode, donated, and supported this initiative. Your "
    "generosity shows what we can achieve together."
)
_RIDE_INVITE = "Next year's edition is a chance for more of us to ride. We hope you join us!"
_RIDE_MERGED = (
    "Thank you to our 19 cycling colleagues, and to everyone who donated and supported "
    "them, for their commitment and generosity. See you next year!"
)
_RIDE_USER_THANKS = (
    "Thank you to our 19 cycling colleagues for their commitment and generosity. See you next year!"
)
_DRIVE_DAY = _TODAY - timedelta(days=3)
_DRIVE = (
    f"The Montréal office blood drive with Héma-Québec takes place on {_spoken(_DRIVE_DAY)} "
    "in room 4B, from 9 a.m. to 3 p.m. Book your slot with Alex Martin."
)

_EN_TO_CA = "Translate into Canadian French: "
_NOTE = f"Remarque{NBSP}: la réunion commence à 14{NBSP}h{NBSP}30 dans la grande salle."
_TIME = f"14{NBSP}h{NBSP}30"
_SUBJECTS = "\n".join(f"Parking update {index}: new rules from October 1" for index in range(20))
_BILINGUAL = (
    "Le bureau de Montréal sera fermé le lundi 13 octobre pour l'Action de grâce. "
    "Le centre d'assistance reste joignable par courriel à aide@example.com.\n\n---\n\n"
    "The Montreal office will be closed on Monday, October 13, for Thanksgiving. "
    "The help desk remains reachable by email at aide@example.com."
)

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="word-upload-then-variety-answer",
        mode="translate",
        turns=(
            Turn("Translate the attached document.", docx=_GREETING_DOCX),
            Turn("Canadian French."),
        ),
        check=_check_docx_after_question,
        good=(
            _answer("Which French variety do you want: Canadian, France, or International?"),
            _docx_establish([f"Bonjour {_link('portail')}.", "Texte de cellule"]),
        ),
        bad=(
            _answer("Which French variety do you want: Canadian, France, or International?"),
            _answer("Here it is.", kind="conversation"),
        ),
    ),
    Scenario(
        name="local-edit-keeps-no-break-spaces",
        mode="revision",
        turns=(
            Turn(f"Proofread this Canadian French text: {_NOTE}"),
            Turn("Replace « réunion » with « rencontre »."),
        ),
        check=_check_local_edit,
        good=(
            _establish(_NOTE, _NOTE),
            ndjson(
                [("deliverable", _NOTE.replace("réunion", "rencontre"))],
                {"operation": "full", "base_version": 1},
            ),
        ),
        bad=(_establish(_NOTE, _NOTE), _answer("Done.")),
    ),
    Scenario(
        name="twenty-subject-lines",
        mode="internal_comms",
        turns=(
            Turn(
                "Give me 20 different email subject lines announcing that the new parking "
                "policy starts October 1."
            ),
        ),
        check=_check_many_subject_lines,
        good=(ndjson([("deliverable", _SUBJECTS)], {"operation": "none"}),),
        bad=(ndjson([("deliverable", "Parking update")], {"operation": "none"}),),
    ),
    Scenario(
        name="bilingual-review-corrects-the-target",
        mode="revision",
        turns=(
            Turn(
                "Check this translation against the original and correct it.\n\n"
                "Original (English): The meeting is postponed to Friday.\n\n"
                "Translation (French): La réunion est annulée vendredi."
            ),
        ),
        check=_check_bilingual_review,
        good=(
            _establish(
                "La réunion est reportée à vendredi.",
                "The meeting is postponed to Friday.\n\n---\n\nLa réunion est annulée vendredi.",
                advice="« Annulée » disait le contraire de « postponed ».",
            ),
        ),
        bad=(
            _establish(
                "La réunion est annulée vendredi.",
                "The meeting is postponed to Friday.\n\n---\n\nLa réunion est annulée vendredi.",
            ),
        ),
    ),
    Scenario(
        name="reference-does-not-override-approved-term",
        mode="revision",
        turns=(
            Turn(
                "Use this document as the style reference.",
                docx=_REFERENCE_DOCX,
                role="reference",
            ),
            Turn(
                "Revise this for clarity: Notre robot conversationnel répond en tout temps "
                "aux questions des employés sur leurs avantages."
            ),
        ),
        check=_check_reference_keeps_approved_term,
        good=(
            _answer("Noted. Share the text you want revised."),
            _establish(
                "Notre robot conversationnel répond en tout temps aux questions des "
                "employés sur leurs avantages.",
                "Notre robot conversationnel répond en tout temps aux questions des "
                "employés sur leurs avantages.",
            ),
        ),
        bad=(
            _answer("Noted. Share the text you want revised."),
            _establish(
                "Notre agent conversationnel répond en tout temps aux questions des "
                "employés sur leurs avantages.",
                "Notre robot conversationnel répond en tout temps aux questions des "
                "employés sur leurs avantages.",
            ),
        ),
    ),
    Scenario(
        name="unrelated-paste-is-a-separate-translation",
        mode="translate",
        turns=(
            Turn(_EN_TO_CA + "The office will be closed on Monday."),
            Turn(_EN_TO_CA + "Please submit your expense reports by Friday."),
        ),
        check=_check_separate_translation,
        good=(
            _establish("Le bureau sera fermé lundi.", "The office will be closed on Monday."),
            _establish(
                "Veuillez soumettre vos notes de frais d'ici vendredi.",
                "Please submit your expense reports by Friday.",
            ),
        ),
        bad=(
            _establish("Le bureau sera fermé lundi.", "The office will be closed on Monday."),
            ndjson(
                [("deliverable", "Veuillez soumettre vos notes de frais d'ici vendredi.")],
                {
                    "operation": "append",
                    "base_version": 1,
                    "source_addition": "Please submit your expense reports by Friday.",
                    "source_separator": "paragraph",
                    "output_separator": "paragraph",
                },
            ),
        ),
    ),
    Scenario(
        name="advice-avoids-markdown-tables",
        mode="translate",
        turns=(
            Turn(
                "What is the best Canadian French term for 'workflow'? Compare the main "
                "options in a table."
            ),
        ),
        check=_check_no_tables,
        good=(
            _answer(
                "- **flux de travaux**: the approved Alithya term.\n"
                "- **flux de travail**: common elsewhere, but not the approved form."
            ),
        ),
        bad=(_answer("| Option | Use |\n|---|---|\n| flux de travaux | approved |"),),
    ),
    Scenario(
        name="pach-becomes-hcbp-and-title-is-english",
        mode="translate",
        turns=(
            Turn(
                "Traduis en anglais : Votre partenaire d'affaires Capital humain (PACH) "
                "communiquera avec vous cette semaine."
            ),
        ),
        check=_check_pach_and_english_title,
        good=(
            _establish(
                "Your Human Capital Business Partner (HCBP) will contact you this week.",
                "Votre partenaire d'affaires Capital humain (PACH) communiquera avec vous "
                "cette semaine.",
            ),
        ),
        bad=(
            _establish(
                "Your human capital business partner (PACH) will contact you this week.",
                "Votre partenaire d'affaires Capital humain (PACH) communiquera avec vous "
                "cette semaine.",
            ),
        ),
        title="HCBP Contact Notice",
        wait_for_title=True,
    ),
    Scenario(
        name="oqlf-typography-and-formats",
        mode="translate",
        turns=(
            Turn(
                _EN_TO_CA + "Note: the session starts at 2:30 p.m. and costs $1,000.50. "
                "Why attend? Because it is free!"
            ),
        ),
        check=_check_oqlf_formats,
        good=(
            _establish(
                f"Remarque{NBSP}: la séance commence à {_TIME} et coûte 1{NBSP}000,50{NBSP}$. "
                "Pourquoi y assister? Parce que c'est gratuit!",
                "Note: the session starts at 2:30 p.m. and costs $1,000.50. Why attend? "
                "Because it is free!",
            ),
        ),
        bad=(
            _establish(
                "Remarque: la séance commence à 14h30 et coûte 1000,50$. Pourquoi y "
                "assister ? Parce que c'est gratuit !",
                "Note: the session starts at 2:30 p.m. and costs $1,000.50. Why attend? "
                "Because it is free!",
            ),
        ),
    ),
    Scenario(
        name="feminine-title-is-flagged-not-guessed",
        mode="translate",
        turns=(
            Turn(
                _EN_TO_CA + "Our President and Chief Executive Officer, Jordan Lee, will "
                "speak at the town hall."
            ),
        ),
        check=_check_feminine_title_is_asked,
        good=(
            _establish(
                "Notre Président et chef de la direction, Jordan Lee, prendra la parole à "
                "l'assemblée générale.",
                "Our President and Chief Executive Officer, Jordan Lee, will speak at the "
                "town hall.",
                advice="Faut-il le titre au féminin (Présidente et cheffe de la direction)?",
            ),
        ),
        bad=(
            _establish(
                "Notre Présidente et cheffe de la direction, Jordan Lee, prendra la parole "
                "à l'assemblée générale.",
                "Our President and Chief Executive Officer, Jordan Lee, will speak at the "
                "town hall.",
            ),
        ),
    ),
    Scenario(
        name="user-choice-overrides-approved-term",
        mode="translate",
        turns=(
            Turn(
                "Translate into Canadian French and keep the English word 'chatbot': Our "
                "chatbot answers HR questions."
            ),
        ),
        check=_check_user_keeps_chatbot,
        good=(
            _establish(
                "Notre chatbot répond aux questions RH.",
                "Our chatbot answers HR questions.",
                advice="The approved term is « robot conversationnel ».",
            ),
        ),
        bad=(
            _establish(
                "Notre robot conversationnel répond aux questions RH.",
                "Our chatbot answers HR questions.",
            ),
        ),
    ),
    Scenario(
        name="bilingual-internal-announcement",
        mode="internal_comms",
        turns=(
            Turn(
                "Draft a short announcement in both English and French: the Montreal "
                "office will be closed on Monday, October 13, for Thanksgiving; the help "
                "desk stays reachable by email at aide@example.com."
            ),
        ),
        check=_check_bilingual_draft,
        good=(_establish(_BILINGUAL, "Office closed October 13; help desk by email."),),
        bad=(
            ndjson(
                [
                    ("deliverable", _BILINGUAL.split("\n\n---\n\n")[1]),
                    ("deliverable", _BILINGUAL.split("\n\n---\n\n")[0]),
                ],
                {"operation": "none"},
            ),
        ),
    ),
    Scenario(
        name="past-event-is-reported-as-done",
        mode="internal_comms",
        turns=(Turn("Draft a short employee news item from these notes. " + _RIDE_NOTES),),
        check=_check_past_event_is_a_recap,
        good=(_establish(f"{_RIDE_BODY}\n\n{_RIDE_MERGED}", _RIDE_NOTES),),
        bad=(
            _establish(
                "Last weekend, 19 cycling colleagues took part in the Make-A-Wish® 48-Hour "
                "Ride and raised over $38,000.\n\nLooking ahead, the 18th edition of the "
                f"48-Hour Ride will take place from {_RIDE} at Aerocity of Mirabel. Mark "
                "your calendar and consider joining us.",
                _RIDE_NOTES,
            ),
        ),
    ),
    Scenario(
        name="terse-correction-rewrites-the-draft",
        mode="internal_comms",
        turns=(
            Turn("Draft a short announcement: " + _DRIVE),
            Turn("It already took place. Make it a thank-you instead: 42 colleagues donated."),
        ),
        check=_check_correction_is_applied,
        good=(
            _answer(
                f"{_spoken(_DRIVE_DAY)} has already passed. Should this be a thank-you "
                "note instead? If so, how many colleagues donated?"
            ),
            _establish(
                "Thank you to the 42 colleagues who gave blood at the Montréal office drive "
                f"with Héma-Québec on {_spoken(_DRIVE_DAY)}. Your donations help patients "
                "across Québec.",
                _DRIVE + " It already took place; 42 colleagues donated.",
            ),
        ),
        bad=(
            _establish(
                f"Give blood on {_spoken(_DRIVE_DAY)}: the Montréal office blood drive with "
                "Héma-Québec runs from 9 a.m. to 3 p.m. in room 4B. Book your slot with "
                "Alex Martin.",
                _DRIVE,
            ),
            ndjson(
                [
                    (
                        "deliverable",
                        f"Give blood on {_spoken(_DRIVE_DAY)}: the Montréal office blood drive "
                        "with Héma-Québec runs from 9 a.m. to 3 p.m. in room 4B. Book your "
                        "slot with Alex Martin.",
                    )
                ],
                {"operation": "full", "base_version": 1},
            ),
        ),
    ),
    Scenario(
        name="user-wording-is-merged-not-duplicated",
        mode="internal_comms",
        turns=(
            Turn(
                "Draft a short employee news item from these notes. "
                + _RIDE_NOTES
                + " End with a paragraph thanking everyone who rode, donated, and supported "
                "the initiative, then a final paragraph inviting colleagues to next year's "
                "edition."
            ),
            Turn(
                "Replace the last paragraph with something like this: Thank you to our 19 "
                "cycling colleagues for their commitment and generosity... see you next year!"
            ),
        ),
        check=_check_user_wording_is_merged,
        good=(
            _establish(f"{_RIDE_BODY}\n\n{_RIDE_THANKS}\n\n{_RIDE_INVITE}", _RIDE_NOTES),
            ndjson(
                [("deliverable", f"{_RIDE_BODY}\n\n{_RIDE_MERGED}")],
                {"operation": "full", "base_version": 1},
            ),
        ),
        bad=(
            _establish(f"{_RIDE_BODY}\n\n{_RIDE_THANKS}\n\n{_RIDE_INVITE}", _RIDE_NOTES),
            ndjson(
                [("deliverable", f"{_RIDE_BODY}\n\n{_RIDE_THANKS}\n\n{_RIDE_USER_THANKS}")],
                {"operation": "full", "base_version": 1},
            ),
        ),
    ),
    Scenario(
        name="word-line-breaks-and-tabs-survive",
        mode="translate",
        turns=(Turn("Translate this document into Canadian French.", docx=_LAYOUT_DOCX),),
        check=_check_word_layout,
        good=(
            _docx_establish(
                [
                    "Bienvenue dans l'équipe.",
                    f"Notre bureau{NBSP}:\n123, rue Main",
                    f"Gestionnaire{NBSP}:\tJordan Lee",
                ]
            ),
        ),
        bad=(
            _docx_establish(
                [
                    "Bienvenue dans l'équipe.",
                    f"Notre bureau{NBSP}: 123, rue Main",
                    f"Gestionnaire{NBSP}: Jordan Lee",
                ]
            ),
        ),
    ),
    Scenario(
        name="side-text-keeps-the-word-document",
        mode="translate",
        turns=(
            Turn("Translate this document into Canadian French.", docx=_LAYOUT_DOCX),
            Turn("Also translate this sentence into Canadian French: See you soon."),
        ),
        check=_check_side_text_keeps_word_work,
        good=(
            _docx_establish(
                [
                    "Bienvenue dans l'équipe.",
                    f"Notre bureau{NBSP}:\n123, rue Main",
                    f"Gestionnaire{NBSP}:\tJordan Lee",
                ]
            ),
            ndjson([("deliverable", "À bientôt.")], {"operation": "none"}),
        ),
        bad=(
            _docx_establish(
                [
                    "Bienvenue dans l'équipe.",
                    f"Notre bureau{NBSP}:\n123, rue Main",
                    f"Gestionnaire{NBSP}:\tJordan Lee",
                ]
            ),
            _establish("À bientôt.", "See you soon."),
        ),
    ),
)


# --- Summary scenario ---------------------------------------------------------------

SUMMARY_GOOD = json.dumps(
    {
        "version": 1,
        "summary": "Canadian French; 'workflow' is always 'flux de travaux'. The workflow "
        "announcement was translated.",
        "unresolved": [],
    }
).encode()
SUMMARY_BAD = json.dumps(
    {"version": 1, "summary": "The workflow announcement was translated.", "unresolved": []}
).encode()


async def summarize(provider: GenerationProvider) -> str:
    """Compact a thread whose earlier summary holds decisions the new turns do not."""

    thread = Thread(
        id="summary-evaluation",
        owner_id="evaluator",
        mode="translate",
        title="Summary evaluation",
        context_summary=(
            "The user chose Canadian French. Always translate 'workflow' as 'flux de travaux'."
        ),
        summary_through_ordinal=2,
    )
    turns = [
        Message(
            id="summary-3",
            thread_id=thread.id,
            ordinal=3,
            role="user",
            actor_user_id="evaluator",
            content_schema_version=1,
            content=[{"type": "conversation", "text": "Translate: The workflow changes."}],
        ),
        Message(
            id="summary-4",
            thread_id=thread.id,
            ordinal=4,
            role="assistant",
            actor_user_id=None,
            content_schema_version=1,
            content=[{"type": "deliverable", "text": "Le flux de travaux change."}],
        ),
    ]
    messages = build_provider_messages(
        thread, purpose="summary", recent_messages=turns, actor_labels={"evaluator": "Charles"}
    )
    request = ProviderRequest(
        generation_id="summary-evaluation",
        purpose="summary",
        mode="translate",
        snapshot={
            "schema_version": 1,
            "mode": "translate",
            "provider_messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
        },
    )
    parts: list[bytes] = []

    async def collect(chunk: bytes) -> None:
        parts.append(chunk)

    await provider.generate(request, collect, asyncio.Event())
    return _parse_context_summary(b"".join(parts))


def check_summary(summary: str) -> None:
    lowered = summary.casefold()
    expect("flux de travaux" in lowered, f"the terminology decision was dropped: {summary}")
    expect("canad" in lowered, f"the locale decision was dropped: {summary}")
