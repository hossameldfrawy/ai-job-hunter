"""
The review card: what a drafted application looks like on each channel, and
how an in-line edit rewrites it.

THE PROBLEM THIS SOLVES
-----------------------
`engine.prepare_application` used to push one card to Telegram and stop there.
That made approval a desk activity: the only way to act on a draft was to open a
terminal and type `python main.py --approve 7`. In practice the draft is read
on a phone, hours before the terminal is next opened, and the posting has often
closed by then.

So the card now goes down BOTH channels and carries its own controls -- reply
"done 7" to submit it, "edit 7 salary: 15000" to change it. Which means the card
has to survive two very different transports:

  TELEGRAM  4096 characters, no encoding, clickable links. Gets the full record:
            every drafted answer, the whole cover letter, the job URL.

  WHATSAPP  a percent-encoded query string with a hard URL ceiling, and
            CallMeBot DROPS what overflows rather than truncating it. Gets a
            short card with no URL that points back at the Telegram one by its
            draft reference.

Both carry the SAME reference -- [DRAFT #7] -- because that reference is the
handle the user quotes back ("done 7"), and an id that differs between channels
is an id that submits the wrong application.

EDITING IS TWO WRITES, NOT ONE
------------------------------
`cover_letter_text` is what the card SHOWS. `submitted_payload_json["fields"]`
is what actually gets typed into the form. `apply_edit` rewrites both from one
instruction, because updating only the first produces a draft that looks edited
and submits the original -- a silent failure of the exact gate this whole flow
exists to be.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone
from typing import Any

from notifier import _clean_field, _over_budget, _strip_control

log = logging.getLogger(__name__)

#: Zero-width sentinel stamped on every card this module emits.
#:
#: The command listener reads the user's Telegram Saved Messages -- which is
#: also where these cards are delivered. Without a marker the listener would
#: read its own review card, see the line 'Reply "done 7" to approve', and have
#: to reason about whether that was a user instruction. It is not: it is the
#: bot quoting itself. Any message carrying this character is ours and is
#: skipped outright. Placed at the FRONT because every transport in this system
#: truncates from the end.
BOT_MARK = "⁣"          # INVISIBLE SEPARATOR

#: Canonical edit targets. The first six line up 1:1 with the semantic `kind`
#: values `auto_apply.browser.inspect_form` assigns, so an edit can find the
#: real input it has to rewrite. The last two are pseudo-fields with no input
#: of their own.
FIELD_COVER_LETTER = "cover_letter"
FIELD_SALARY = "salary"
FIELD_EXPERIENCE = "years_experience"
FIELD_NOTICE = "notice_period"
FIELD_PHONE = "phone"
FIELD_LOCATION = "location"
FIELD_ANSWER = "answer"      # one specific screening answer, by number
FIELD_NOTE = "note"          # free-form instruction, no named target

EDITABLE_FIELDS = (
    FIELD_COVER_LETTER, FIELD_SALARY, FIELD_EXPERIENCE, FIELD_NOTICE,
    FIELD_PHONE, FIELD_LOCATION,
)


def draft_ref(app_id: int | str) -> str:
    """The handle both cards print and the user quotes back."""
    return f"[DRAFT #{app_id}]"


# ---------------------------------------------------------------------------
# One draft, normalised
# ---------------------------------------------------------------------------
class DraftCard:
    """Everything a review card shows, from either of its two sources.

    A draft is rendered twice in its life: once at drafting time, when it exists
    only as a Gemini response plus an `Evaluation`, and again after every edit,
    when it exists only as a row in `applications_history`. Rendering those two
    shapes through two code paths is how the "before" and "after" cards drift
    out of sync, so both are normalised into this first.
    """

    __slots__ = (
        "app_id", "company", "role", "platform", "match_score", "salary",
        "experience", "answers", "cover_letter", "job_url", "status",
        "revision", "form_ok", "form_note", "notes",
    )

    def __init__(
        self, *, app_id: int, company: str = "", role: str = "",
        platform: str = "", match_score: int | None = None, salary: str = "",
        experience: str = "", answers: list[dict[str, Any]] | None = None,
        cover_letter: str = "", job_url: str = "", status: str = "",
        revision: int = 0, form_ok: bool = False, form_note: str = "",
        notes: list[str] | None = None,
    ) -> None:
        self.app_id = int(app_id)
        self.company = str(company or "Unknown")
        self.role = str(role or "Unknown")
        self.platform = str(platform or "unknown")
        # None, not 0. A draft written before the score was stored alongside it
        # has no score -- and "0%" on a card whose entire job is to help you
        # decide is not a missing value, it is a wrong one.
        self.match_score = None if match_score is None else int(match_score)
        self.salary = str(salary or "")
        self.experience = str(experience or "")
        self.answers = [a for a in (answers or []) if isinstance(a, dict)]
        self.cover_letter = str(cover_letter or "")
        self.job_url = str(job_url or "")
        self.status = str(status or "")
        self.revision = int(revision or 0)
        self.form_ok = bool(form_ok)
        self.form_note = str(form_note or "")
        self.notes = list(notes or [])

    # -- constructors -------------------------------------------------------
    @classmethod
    def from_draft(
        cls, app_id: int, ev: Any, draft: dict[str, Any], platform: str = "",
        *, experience: str = "", form_ok: bool = False, form_note: str = "",
    ) -> "DraftCard":
        """Build from the live objects, at drafting time."""
        return cls(
            app_id=app_id,
            company=getattr(ev, "company_name", "") or "",
            role=getattr(ev, "role_title", "") or "",
            platform=platform or getattr(ev, "source_platform", "") or "",
            match_score=int(getattr(ev, "match_score", 0) or 0),
            salary=str(draft.get("salary_expectation") or ""),
            experience=experience,
            answers=list(draft.get("answers") or []),
            cover_letter=str(draft.get("cover_letter") or ""),
            job_url=getattr(ev, "direct_link", "") or "",
            form_ok=form_ok,
            form_note=form_note,
        )

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "DraftCard":
        """Rebuild from `applications_history`, after an edit or on demand.

        Tolerates every shape the column has ever held: a legacy draft with no
        `form_ok`, a row whose payload is empty, and one whose payload is not
        valid JSON at all. A card that refuses to render is a draft the user
        cannot act on.
        """
        payload = payload_of(row)
        draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else {}
        # The stored cover_letter_text column is authoritative -- it is what the
        # edit engine writes -- and the copy inside `draft` is the fallback for
        # rows written before the column existed.
        cover = row.get("cover_letter_text") or draft.get("cover_letter") or ""
        return cls(
            app_id=int(row.get("id") or 0),
            company=row.get("company") or "",
            role=row.get("role") or "",
            platform=row.get("platform") or "",
            match_score=_optional_int(payload.get("match_score")),
            salary=str(draft.get("salary_expectation") or ""),
            experience=str(payload.get("experience") or ""),
            answers=list(draft.get("answers") or []),
            cover_letter=cover,
            job_url=row.get("job_url") or "",
            status=row.get("status") or "",
            revision=int(row.get("revision") or 0),
            form_ok=payload.get("form_ok") is True,
            form_note=str(payload.get("form_note") or ""),
            notes=[str(n) for n in (payload.get("notes") or [])],
        )

    # -- helpers ------------------------------------------------------------
    @property
    def ref(self) -> str:
        return draft_ref(self.app_id)

    @property
    def needs_you(self) -> int:
        """How many drafted answers Gemini itself flagged as guesses."""
        return sum(1 for a in self.answers if a.get("confident") is False)


def _optional_int(value: Any) -> int | None:
    """int(value), or None when there is genuinely nothing recorded."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def payload_of(row: dict[str, Any]) -> dict[str, Any]:
    """The stored payload as a dict, whatever state the column is in."""
    raw = row.get("submitted_payload_json") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("Application #%s has an unreadable payload; treating it as "
                    "empty.", row.get("id"))
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# Rendering -- Telegram (the full record)
# ---------------------------------------------------------------------------
def format_review_telegram(card: DraftCard) -> str:
    """The full card. 4096 characters, real links, every drafted answer."""
    lines = [
        f"{BOT_MARK}\U0001F4DD *APPLICATION DRAFT READY FOR REVIEW* {card.ref}",
        "",
        f"\U0001F3E2 Company:  {_clean_field(card.company, 120)}",
        f"\U0001F4BC Role:     {_clean_field(card.role, 160)}",
        f"\U0001F310 Platform: {_clean_field(card.platform, 60)}",
        "\U0001F4CA Match:    "
        + (f"{card.match_score}%" if card.match_score is not None
           else "(not recorded on this draft)"),
    ]
    if card.revision:
        lines.append(f"✏️ Revision: {card.revision} edit(s) applied")
    if card.job_url:
        lines.append(f"\U0001F517 Link:     {card.job_url}")

    lines += ["", "\U0001F4CB *PROPOSED FORM ANSWERS*"]
    lines.append(f"\U0001F4B0 Salary:     {card.salary or '(not asked / not stated)'}")
    lines.append(f"\U0001F9EE Experience: {card.experience or '(not asked)'}")

    if card.answers:
        for index, answer in enumerate(card.answers[:8], 1):
            flag = "" if answer.get("confident", True) else "   ⚠️ NEEDS YOU"
            lines.append(
                f"{index}. {str(answer.get('question', ''))[:90]}{flag}\n"
                f"   → {str(answer.get('answer', ''))[:220]}"
            )
        if len(card.answers) > 8:
            lines.append(f"   ... and {len(card.answers) - 8} more")
    else:
        lines.append("(no screening questions on this form)")

    lines += ["", "✍️ *TAILORED COVER LETTER*",
              _clean_field(card.cover_letter, 1400) or "(empty)"]

    if card.notes:
        lines += ["", "\U0001F5D2️ *YOUR NOTES*"]
        lines += [f"• {_clean_field(n, 180)}" for n in card.notes[-4:]]

    if not card.form_ok:
        lines += [
            "",
            "⚠️ *NO AUTO-SUBMITTABLE FORM FOUND* — "
            + (_clean_field(card.form_note, 200) or "the page was never checked"),
            "The cover letter above is ready to paste. Apply by hand here:",
            card.job_url or "(no link in the posting)",
        ]

    lines += [
        "",
        "─" * 18,
        f"✅ Approve & submit:  done {card.app_id}   |   "
        f"موافق {card.app_id}",
        f"✏️ Change salary:     edit {card.app_id} salary: 15000 AED",
        f"✏️ Rewrite letter:    edit {card.app_id} cover letter: <new text>",
        f"✏️ بالعربي:         "
        f"تعديل {card.app_id} "
        f"الراتب: 15000",
        f"❌ Discard:           decline {card.app_id}   |   "
        f"رفض {card.app_id}",
        f"\U0001F4BB From a terminal:   python main.py --approve {card.app_id}"
        f"   /   --decline {card.app_id}",
    ]
    return _strip_control("\n".join(lines))[:4000]


# ---------------------------------------------------------------------------
# Rendering -- WhatsApp (short, no URL, points at Telegram)
# ---------------------------------------------------------------------------
def format_review_whatsapp(card: DraftCard, telegram_delivered: bool = True) -> str:
    """The short card, built to survive CallMeBot's percent-encoded URL budget.

    No application URL, for the same reason the job alert has none: job links
    run past 400 characters, encode badly, and CallMeBot drops an over-long
    message instead of trimming it. The reader gets the decision-critical facts
    and the reply syntax; the full card is one search away in Saved Messages.

    The cover-letter preview is the only elastic part, so it is what shrinks --
    metadata and the reply instructions are never sacrificed, because a card
    that fits but cannot be answered is worse than no card at all.
    """
    company = _clean_field(card.company, 70)
    role = _clean_field(card.role, 90)
    platform = _clean_field(card.platform, 40)
    salary = _clean_field(card.salary, 60)
    experience = _clean_field(card.experience, 40)
    preview = _clean_field(card.cover_letter, 400)

    def build(letter: str) -> str:
        head = [
            f"{BOT_MARK}\U0001F4DD *DRAFT READY FOR REVIEW* {card.ref}"
            + (f" ({card.match_score}%)" if card.match_score is not None
               else ""),
            f"\U0001F3E2 *Company:* {company or 'Unknown'}",
            f"\U0001F4BC *Role:* {role or 'Unknown'}",
            f"\U0001F310 *Platform:* {platform or 'unknown'}",
        ]
        answers_line = (
            f"\U0001F4CB *Answers:* {len(card.answers)} drafted"
            + (f", {card.needs_you} need you" if card.needs_you else "")
            if card.answers else "\U0001F4CB *Answers:* none asked"
        )
        body = [
            f"\U0001F4B0 *Salary:* {salary or 'not asked'}",
            f"\U0001F9EE *Experience:* {experience or 'not asked'}",
            answers_line,
        ]
        if not card.form_ok:
            body.append("⚠️ *No auto-submit form* — apply by hand")
        if letter:
            body.append(f"✍️ {letter}")

        tail = [
            f"✅ Reply *done {card.app_id}* to submit",
            f"✏️ Or *edit {card.app_id} salary: 15000*",
        ]
        tail.append(
            f"\U0001F50E Full card: Telegram Saved Messages {card.ref}"
            if telegram_delivered else
            "⚠️ Telegram copy did not arrive — see the run log"
        )
        return _strip_control(
            "\n".join(head) + "\n\n" + "\n".join(body) + "\n\n" + "\n".join(tail)
        )

    while preview and _over_budget(build(preview)):
        preview = _shrink(preview)
    return build(preview)


def _shrink(text: str) -> str:
    """Drop roughly a quarter of a field, or clear it once it is a stub."""
    if len(text) <= 32:
        return ""
    return text[: int(len(text) * 0.75)].rstrip(" ,;،") + "..."


# ---------------------------------------------------------------------------
# Rendering -- outcomes
# ---------------------------------------------------------------------------
def _evidence_name(path: str) -> str:
    """Just the filename.

    Split on BOTH separators rather than just the backslash: the screenshot is
    captured on Windows and the vault is readable from Linux, and a
    one-separator split prints the entire absolute path on the other platform.
    pathlib is no help -- PurePosixPath does not treat "\\" as a separator.
    """
    if not path:
        return ""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def format_submitted_telegram(app_id: int, app: dict[str, Any]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    shot = _evidence_name(str(app.get("screenshot_path") or ""))
    lines = [
        f"{BOT_MARK}\U0001F680 *APPLICATION SUCCESSFULLY SUBMITTED!* "
        f"{draft_ref(app_id)}",
        "",
        f"\U0001F3E2 Company:  {_clean_field(str(app.get('company') or ''), 120)}",
        f"\U0001F4BC Role:     {_clean_field(str(app.get('role') or ''), 160)}",
        f"\U0001F310 Platform: {_clean_field(str(app.get('platform') or ''), 60)}",
        "\U0001F4F8 Evidence: "
        + (f"screenshot saved — {shot}" if shot else "not captured"),
        f"\U0001F552 Time:     {stamp}",
    ]
    if app.get("job_url"):
        lines.append(f"\U0001F517 Link:     {app['job_url']}")
    lines += ["", "Recorded as *submitted* in your application history."]
    return _strip_control("\n".join(lines))[:4000]


def format_submitted_whatsapp(app_id: int, app: dict[str, Any]) -> str:
    shot = _evidence_name(str(app.get("screenshot_path") or ""))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return _strip_control("\n".join([
        f"{BOT_MARK}\U0001F680 *APPLICATION SUBMITTED* {draft_ref(app_id)}",
        f"\U0001F3E2 *Company:* {_clean_field(str(app.get('company') or ''), 70)}",
        f"\U0001F4BC *Role:* {_clean_field(str(app.get('role') or ''), 90)}",
        f"\U0001F310 *Platform:* {_clean_field(str(app.get('platform') or ''), 40)}",
        "\U0001F4F8 *Evidence:* " + (shot or "not captured"),
        f"\U0001F552 {stamp}",
        "",
        f"\U0001F50E Screenshot is on Telegram Saved Messages {draft_ref(app_id)}",
    ]))


def format_failure_telegram(app_id: int, app: dict[str, Any], reason: str) -> str:
    return _strip_control("\n".join([
        f"{BOT_MARK}⚠️ *APPLICATION FAILED* {draft_ref(app_id)}",
        f"\U0001F3E2 {_clean_field(str(app.get('company') or ''), 90)} — "
        f"{_clean_field(str(app.get('role') or ''), 120)}",
        f"Reason: {_clean_field(reason, 400)}",
        "",
        "The draft is kept, so nothing is lost. Apply by hand here:",
        str(app.get("job_url") or "(no link recorded)"),
    ]))[:4000]


def format_failure_whatsapp(app_id: int, app: dict[str, Any], reason: str) -> str:
    return _strip_control("\n".join([
        f"{BOT_MARK}⚠️ *APPLICATION FAILED* {draft_ref(app_id)}",
        f"\U0001F3E2 *{_clean_field(str(app.get('company') or ''), 60)}* — "
        f"{_clean_field(str(app.get('role') or ''), 70)}",
        f"Reason: {_clean_field(reason, 180)}",
        "",
        f"\U0001F50E Details on Telegram Saved Messages {draft_ref(app_id)}",
    ]))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def enabled_channels() -> tuple[str, ...]:
    """Which channels `hitl.channels` leaves switched on. Both, by default.

    Read here rather than in the notifier: the notifier is generic transport
    and should not know what a review card is. Turning both off is treated as
    a mistake and ignored -- a review gate nobody is told about is a gate that
    stalls forever.
    """
    from config import settings

    cfg = ((settings.raw.get("hitl", {}) or {}).get("channels", {}) or {})
    on = tuple(name for name in ("telegram", "whatsapp")
               if cfg.get(name, True))
    return on or ("telegram", "whatsapp")


def dispatch_text(notifier: Any, telegram_text: str, whatsapp_text: str,
                  photo: str = "", caption: str = "") -> Any:
    """Send on both channels, tolerating a notifier that only knows one.

    `send_dual` is the real route. The fallback matters for the recording fakes
    used in tests and for any caller still holding an older notifier: losing the
    review card entirely because a helper method is missing would be a far worse
    failure than sending the long card to WhatsApp.
    """
    if notifier is None:
        return None
    if hasattr(notifier, "send_dual"):
        return notifier.send_dual(telegram_text, whatsapp_text, photo=photo,
                                  photo_caption=caption,
                                  channels=enabled_channels())
    notifier.send_via_telegram(telegram_text)
    return None


def dispatch_review(notifier: Any, card: DraftCard) -> Any:
    """Push a draft (new or freshly edited) to BOTH channels."""
    return dispatch_text(
        notifier,
        format_review_telegram(card),
        format_review_whatsapp(card),
    )


def dispatch_submitted(notifier: Any, app_id: int, app: dict[str, Any]) -> Any:
    """Confirm a submission on both channels, with the screenshot on Telegram."""
    shot = str(app.get("screenshot_path") or "")
    caption = (f"{draft_ref(app_id)} {app.get('company') or ''} — "
               f"{app.get('role') or ''}").strip()
    return dispatch_text(
        notifier,
        format_submitted_telegram(app_id, app),
        format_submitted_whatsapp(app_id, app),
        photo=shot,
        caption=caption,
    )


def dispatch_failure(notifier: Any, app_id: int, app: dict[str, Any],
                     reason: str) -> Any:
    return dispatch_text(
        notifier,
        format_failure_telegram(app_id, app, reason),
        format_failure_whatsapp(app_id, app, reason),
    )


# ---------------------------------------------------------------------------
# In-line editing
# ---------------------------------------------------------------------------
class EditResult:
    """What an edit changed, in both halves of the record."""

    __slots__ = ("ok", "payload", "cover_letter", "description", "selectors",
                 "warning")

    def __init__(self, ok: bool, payload: dict[str, Any],
                 cover_letter: str | None = None, description: str = "",
                 selectors: list[str] | None = None, warning: str = "") -> None:
        self.ok = ok
        self.payload = payload
        self.cover_letter = cover_letter
        self.description = description
        self.selectors = selectors or []
        self.warning = warning

    def __repr__(self) -> str:                       # pragma: no cover - debug
        return (f"EditResult(ok={self.ok!r}, description={self.description!r}, "
                f"selectors={self.selectors!r}, warning={self.warning!r})")


def field_map(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The inspected form, as stored at drafting time. [] for legacy drafts."""
    raw = payload.get("field_map")
    return [f for f in raw if isinstance(f, dict)] if isinstance(raw, list) else []


def _selectors_for_kind(payload: dict[str, Any], kind: str) -> list[str]:
    return [
        str(f.get("selector"))
        for f in field_map(payload)
        if f.get("kind") == kind and f.get("selector")
    ]


def _selector_for_label(payload: dict[str, Any], label: str) -> str:
    target = (label or "").strip().lower()
    if not target:
        return ""
    for entry in field_map(payload):
        if str(entry.get("label", "")).strip().lower() == target:
            return str(entry.get("selector") or "")
    return ""


def apply_edit(
    payload: dict[str, Any], field: str, value: str, *,
    answer_index: int | None = None, previous_cover: str = "",
) -> EditResult:
    """Rewrite a stored draft. Pure: takes a payload, returns a new one.

    Every write goes through here so the two halves of a draft cannot diverge.
    Anything that changes the cover letter changes `fields[<cover selector>]`
    too; anything that changes the salary changes `fields[<salary selector>]`;
    a re-answered screening question changes the field its own label maps to.

    When the form has no input of that kind -- common: most job pages never ask
    for a salary -- the value is still recorded on the draft and a WARNING is
    returned. That combination is deliberate. Refusing the edit would lose the
    user's decision; applying it silently would imply a form field that does not
    exist. Saying "recorded, but this form never asks" is the honest answer.
    """
    payload = copy.deepcopy(payload if isinstance(payload, dict) else {})
    fields: dict[str, Any] = payload.setdefault("fields", {})
    draft: dict[str, Any] = payload.setdefault("draft", {})
    value = value.strip()

    if not field:
        return EditResult(False, payload, None,
                          "no field named in the edit instruction")

    # -- free-form note: recorded, never silently applied to a form input ----
    if field == FIELD_NOTE:
        if not value:
            return EditResult(False, payload, None, "the note was empty")
        notes = payload.setdefault("notes", [])
        if isinstance(notes, list):
            notes.append(value[:500])
        return EditResult(True, payload, None, f"note recorded: {value[:80]}")

    # -- one specific screening answer, by its number on the card -----------
    if field == FIELD_ANSWER:
        answers = draft.get("answers")
        if not isinstance(answers, list) or not answers:
            return EditResult(False, payload, None,
                              "this draft has no screening answers to edit")
        if answer_index is None or not (1 <= answer_index <= len(answers)):
            return EditResult(
                False, payload, None,
                f"answer number must be between 1 and {len(answers)}",
            )
        target = answers[answer_index - 1]
        if not isinstance(target, dict):
            return EditResult(False, payload, None, "that answer is unreadable")
        if not value:
            return EditResult(False, payload, None,
                              "refusing to blank a screening answer")
        target["answer"] = value
        # The user just wrote it themselves, so it is no longer a model guess.
        target["confident"] = True
        selector = _selector_for_label(payload, str(target.get("question", "")))
        if selector:
            fields[selector] = value
        return EditResult(
            True, payload, None,
            f"answer {answer_index} rewritten "
            f"({str(target.get('question', ''))[:60]})",
            [selector] if selector else [],
            "" if selector else ("recorded on the draft; this form has no input "
                                 "matching that question"),
        )

    # -- cover letter -------------------------------------------------------
    if field == FIELD_COVER_LETTER:
        if not value:
            return EditResult(False, payload, None,
                              "refusing to replace the cover letter with nothing")
        draft["cover_letter"] = value
        selectors = _selectors_for_kind(payload, FIELD_COVER_LETTER)
        if not selectors:
            # Legacy drafts predate `field_map`. Fall back to matching the text
            # actually sitting in the payload -- imperfect, but it beats
            # submitting the old letter.
            selectors = [
                sel for sel, current in fields.items()
                if previous_cover and str(current) == previous_cover
            ]
        for selector in selectors:
            fields[selector] = value
        return EditResult(
            True, payload, value,
            f"cover letter replaced ({len(value)} characters)",
            selectors,
            "" if selectors else ("recorded on the draft; no cover-letter input "
                                  "was detected on this form"),
        )

    # -- any other real form field ------------------------------------------
    if not value:
        return EditResult(False, payload, None,
                          f"no new value given for {field.replace('_', ' ')}")
    if field == FIELD_SALARY:
        draft["salary_expectation"] = value
    if field == FIELD_EXPERIENCE:
        payload["experience"] = value

    selectors = _selectors_for_kind(payload, field)
    for selector in selectors:
        fields[selector] = value
    return EditResult(
        True, payload, None,
        f"{field.replace('_', ' ')} set to {value[:80]!r}",
        selectors,
        "" if selectors else (f"recorded on the draft; this form has no "
                              f"{field.replace('_', ' ')} input"),
    )


# ---------------------------------------------------------------------------
# AI-assisted free-form revision
# ---------------------------------------------------------------------------
REVISE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "cover_letter": {
            "type": "STRING",
            "description": (
                "The FULL revised cover letter, not a diff and not a commentary. "
                "Keep the candidate's own facts; change only what the "
                "instruction asks for."
            ),
        }
    },
    "required": ["cover_letter"],
}

REVISE_INSTRUCTION = """\
You are revising ONE cover letter on the candidate's own instruction.

Apply the instruction and return the complete revised letter.

RULES:
  * Never add experience, employers, certifications or years that are not
    already in the letter. The instruction may change tone, length, emphasis or
    wording -- it may not invent facts.
  * Keep the candidate's language: if the letter is Arabic, stay in Arabic.
  * Return the whole letter. Not a diff, not an explanation, not a preamble.
"""


def revise_cover_letter(current: str, instruction: str,
                        card: "DraftCard | None" = None) -> str:
    """Apply a free-form instruction to a cover letter. "" if it cannot.

    Returning "" rather than raising is the contract: a failed revision must
    degrade to "your note was recorded", never to a lost draft or a listener
    that dies on the next message.
    """
    if not current.strip() or not instruction.strip():
        return ""
    try:
        from evaluator import GeminiEvaluator

        context = ""
        if card is not None:
            context = (f"Company: {card.company}\nRole: {card.role}\n"
                       f"Platform: {card.platform}\n\n")
        evaluator = GeminiEvaluator()
        payload, _model = evaluator._generate({          # noqa: SLF001
            "systemInstruction": {"parts": [{"text": REVISE_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [{"text":
                f"{context}=== CURRENT COVER LETTER ===\n{current[:4000]}\n\n"
                f"=== INSTRUCTION ===\n{instruction[:800]}\n"
            }]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": REVISE_SCHEMA,
                "temperature": 0.3,
                "maxOutputTokens": 2048,
            },
        })
        revised = str(evaluator._extract_json(payload).get("cover_letter") or "")
        return revised.strip()
    except Exception as exc:
        log.warning("Could not revise the cover letter with the model: %s: %s",
                    type(exc).__name__, exc)
        return ""
