"""
Human-in-the-loop application engine.

    draft  ->  review dispatch  ->  YOUR approval  ->  submit  ->  evidence

The approval step is a real gate, not a notification. Gemini writes the cover
letter and the screening answers; those go to BOTH Telegram and WhatsApp, and
the application sits at `review_pending` until you approve it. Nothing is ever
submitted in your name on the strength of a model's guess about your salary
expectations.

Approval arrives by whichever route you have to hand: `python main.py --approve
7` from a terminal, or a reply of "done 7" / "موافق ٧" read straight out of
Telegram Saved Messages by `auto_apply.control`. Both land in
`submit_application`, so the gate is one gate however it is opened.

Two things are stored at draft time purely so the flow can be reopened later:
`field_map` (what each selector on the form actually is) and `experience`.
Without the first, an in-line edit can only change the draft's own copy of the
text while the ORIGINAL value still goes into the form.

LinkedIn is excluded at the top of every entry point, not filtered somewhere
downstream, so no future refactor can quietly start automating it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from config import settings
from models import Evaluation
from vault import (
    STATUS_APPROVED, STATUS_DECLINED, STATUS_DRAFT, STATUS_FAILED,
    STATUS_REVIEW, STATUS_SUBMITTED, SecureStore,
)

log = logging.getLogger(__name__)

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "cover_letter": {
            "type": "STRING",
            "description": (
                "150-200 words, first person, addressed to the hiring team. "
                "Name the specific overlapping technologies from the posting and "
                "the CV. No flattery, no 'I am writing to apply', no invented "
                "experience."
            ),
        },
        "answers": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "question": {"type": "STRING"},
                    "answer": {
                        "type": "STRING",
                        "description": (
                            "Concise and factual, grounded ONLY in the CV. If the "
                            "CV does not support an answer, say so plainly rather "
                            "than inventing experience."
                        ),
                    },
                    "confident": {
                        "type": "BOOLEAN",
                        "description": (
                            "false if the CV does not really answer this and a "
                            "human should write it instead."
                        ),
                    },
                },
                "required": ["question", "answer", "confident"],
            },
        },
        "salary_expectation": {
            "type": "STRING",
            "description": (
                "Only if the form asks. Base it on the posting's own range when "
                "given; otherwise return an empty string -- do NOT invent a "
                "number, it is binding on the candidate."
            ),
        },
    },
    "required": ["cover_letter", "answers", "salary_expectation"],
}

DRAFT_INSTRUCTION = """\
You are helping ONE specific candidate apply for ONE specific job. You are
given their CV, the job posting, and the questions the application form asks.

Write a tailored cover letter and answer each question.

RULES:
  * Ground everything in the CV. You may connect and rephrase what is there;
    you may never add experience, employers, certifications or years.
  * If a question cannot be answered honestly from the CV, set confident=false
    and write what the candidate would need to fill in. Do not bluff.
  * Be concrete. Name the actual technologies that overlap.
  * No filler openings ("I am writing to express my interest"), no superlatives.
  * Match the language of the posting: if it is Arabic, answer in Arabic.
"""


class ApplyError(RuntimeError):
    pass


def _cfg() -> dict[str, Any]:
    return settings.raw.get("auto_apply", {}) or {}


def is_automatable(source_platform: str) -> tuple[bool, str]:
    """Whether this platform may be driven by a browser at all.

    LinkedIn is permanently excluded. Its anti-automation enforcement is the
    strictest of any source here, and it is also the single most productive one
    -- a flagged account would cost far more than the applications it saved.
    """
    platform = (source_platform or "").split(":")[0].strip().lower()
    for banned in _cfg().get("never_automate", ["linkedin"]):
        if str(banned).lower() in platform:
            return False, (
                f"{platform} is never automated: applications there are "
                f"manual-only, by design."
            )
    return True, ""


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------
# The questions every ATS asks, whether or not this particular page showed
# them to the inspector.
#
# Why answer questions nobody asked yet: a multi-step form (Workday, Taleo)
# only renders page two AFTER page one is submitted, so the inspector never
# sees them at draft time -- and by the time it does, the browser is mid-flow
# with no human watching and no model call budgeted. Drafting the standard set
# up front means the wizard walker has an answer ready when the question
# appears, instead of leaving a required field blank and failing at validation.
#
# They are also exactly the questions a human would want to review BEFORE
# approving, which is the whole point of the review card.
STANDARD_SCREENING_QUESTIONS: tuple[str, ...] = (
    "What is your notice period?",
    "What is your expected salary?",
    "How many years of experience do you have with VoIP, SIP and Asterisk?",
    "How many years of experience do you have with Python?",
    "How many years of experience do you have with AI or machine learning?",
    "Do you require visa sponsorship to work in this location?",
    "Are you legally authorised to work in the job's country?",
    "When can you start?",
    "Are you willing to relocate?",
    "What is your current location?",
)


def draft_answers(
    ev: Evaluation, job_description: str, questions: list[str]
) -> dict[str, Any]:
    """Ask Gemini for a cover letter and one answer per screening question.

    The form's OWN questions come first and are answered verbatim. The standard
    set is appended so a multi-step form has answers ready for pages the
    inspector could not see yet -- see STANDARD_SCREENING_QUESTIONS.
    """
    asked = [q for q in questions if q and q.strip()]
    seen = {q.strip().lower() for q in asked}
    questions = asked + [
        q for q in STANDARD_SCREENING_QUESTIONS if q.lower() not in seen
    ]
    from auto_apply.candidate import load_candidate
    from cv_profile import load_cv
    from evaluator import GeminiEvaluator

    candidate = load_candidate()
    listed = "\n".join(f"  {i + 1}. {q}" for i, q in enumerate(questions)) or "  (none)"
    prompt = (
        f"=== CANDIDATE CV ===\n{load_cv().to_prompt()}\n\n"
        f"=== CANDIDATE HIGHLIGHTS ===\n{candidate.highlights()}\n\n"
        f"=== JOB ===\n"
        f"Company: {ev.company_name}\nRole: {ev.role_title}\n"
        f"Location: {ev.location}\nSalary in posting: {ev.salary or 'not stated'}\n"
        f"Why it matched: {ev.why_matched}\n"
        f"Description:\n{(job_description or '(not captured)')[:4000]}\n\n"
        f"=== FORM QUESTIONS ===\n{listed}\n"
    )

    evaluator = GeminiEvaluator()
    payload, model = evaluator._generate({          # noqa: SLF001
        "systemInstruction": {"parts": [{"text": DRAFT_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": ANSWER_SCHEMA,
            "temperature": 0.3,
            # 3072 was not enough and the failure was ugly: an ARABIC cover
            # letter for a Saudi posting hit MAX_TOKENS, the response came back
            # as JSON cut off mid-string, and the whole draft was lost with an
            # "unparseable JSON" error. Arabic costs roughly three times the
            # tokens of the equivalent English, and a bilingual pipeline writes
            # Arabic whenever the posting is in Arabic -- which on the Gulf
            # boards is most of the time. Output tokens are only billed when
            # used, so the headroom is free.
            "maxOutputTokens": 8192,
        },
    })
    draft = evaluator._extract_json(payload)        # noqa: SLF001
    draft["_model"] = model
    return draft


def describe_fields(fields: list[Any]) -> list[dict[str, Any]]:
    """Record WHAT each selector is, alongside the value we put in it.

    `build_payload` returns `{selector: value}` and nothing else, which is all a
    submission needs -- and not enough for an edit. When the user later says
    "edit 7 salary: 15000", the engine has to find the salary input on a form it
    is not looking at, and a bare selector string carries no clue which one that
    is. Without this map an in-line edit can only update the draft's own copy of
    the text, and the ORIGINAL value still goes into the form: an edit that
    looks applied and submits the old answer.
    """
    return [
        {
            "selector": f.selector,
            "kind": f.kind,
            "label": f.label,
            "input_type": f.input_type,
            "required": bool(getattr(f, "required", False)),
        }
        for f in fields
    ]


def build_payload(
    ev: Evaluation, draft: dict[str, Any], fields: list[Any]
) -> dict[str, str]:
    """Map the candidate profile + drafted answers onto the detected fields."""
    from auto_apply.candidate import load_candidate

    values = load_candidate().form_values()
    payload: dict[str, str] = {}
    answers = {
        str(a.get("question", "")).strip(): str(a.get("answer", ""))
        for a in draft.get("answers", []) if isinstance(a, dict)
    }

    for f in fields:
        if f.kind == "resume":
            continue                      # handled separately as a file upload
        if f.kind == "cover_letter":
            payload[f.selector] = draft.get("cover_letter", "")
        elif f.kind == "salary":
            payload[f.selector] = draft.get("salary_expectation", "") or ""
        elif f.kind in values and values[f.kind]:
            payload[f.selector] = values[f.kind]
        elif f.is_question:
            payload[f.selector] = (
                answers.get(f.label.strip(), "")
                or match_screening_answer(f.label, answers)
            )
    return {k: v for k, v in payload.items() if v}


#: Distinctive words that identify a standard screening question, so an answer
#: drafted for our wording still lands on the board's wording. "Notice period
#: (in weeks)" and "What is your notice period?" are the same question.
_SCREENING_TOPICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("notice", ("notice",)),
    ("salary", ("salary", "compensation", "expected pay", "ctc")),
    ("voip", ("voip", "sip", "asterisk", "pbx", "telephony")),
    ("python", ("python",)),
    ("ai", ("machine learning", "artificial intelligence", " ai ")),
    ("sponsorship", ("sponsor", "visa")),
    ("authorised", ("authorised", "authorized", "eligible to work",
                    "right to work")),
    ("start", ("start date", "when can you start", "availability")),
    ("relocate", ("relocat",)),
    ("location", ("current location", "where are you based", "city")),
)


def _topic_of(label: str) -> str:
    blob = f" {(label or '').lower()} "
    for topic, needles in _SCREENING_TOPICS:
        if any(n in blob for n in needles):
            return topic
    return ""


def match_screening_answer(label: str, answers: dict[str, str]) -> str:
    """Find a drafted answer for a question worded differently from ours.

    A multi-step form asks "Notice period (weeks)" on page three; the draft
    holds "What is your notice period?". Exact-match leaves the field blank and
    the form fails validation, so questions are matched by TOPIC instead --
    conservatively, and only for the standard set, because a wrong answer to a
    screening question is worse than an empty one a human then fills in.
    """
    topic = _topic_of(label)
    if not topic:
        return ""
    for question, answer in answers.items():
        if answer and _topic_of(question) == topic:
            return answer
    return ""


# ---------------------------------------------------------------------------
# Review dispatch
#
# The cards themselves live in `auto_apply.review`, which renders one draft for
# BOTH channels and is also what re-renders it after every in-line edit. These
# two functions stay here as the names the rest of the system already calls.
# ---------------------------------------------------------------------------
def format_review_message(
    app_id: int, ev: Evaluation, draft: dict[str, Any], platform: str,
    *, experience: str = "", form_ok: bool = True, form_note: str = "",
) -> str:
    """The full Telegram review card for a freshly drafted application."""
    from auto_apply.review import DraftCard, format_review_telegram

    return format_review_telegram(DraftCard.from_draft(
        app_id, ev, draft, platform,
        experience=experience, form_ok=form_ok, form_note=form_note,
    ))


def format_submitted_message(app_id: int, app: dict[str, Any]) -> str:
    """The full Telegram confirmation card for a completed submission."""
    from auto_apply.review import format_submitted_telegram

    return format_submitted_telegram(app_id, app)


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------
def prepare_application(
    ev: Evaluation, store: SecureStore, notifier: Any,
    job_description: str = "",
) -> int | None:
    """Inspect the form, draft the answers, and park it for review.

    Returns the application id, or None if this job should not be automated.
    """
    allowed, reason = is_automatable(ev.source_platform)
    if not allowed:
        log.info("Skipping auto-apply for %r: %s", ev.role_title[:50], reason)
        return None

    threshold = int(_cfg().get("min_score", 80))
    if ev.match_score < threshold:
        log.info("Score %d%% below the auto-apply bar of %d%%.",
                 ev.match_score, threshold)
        return None

    existing = store.application_for_fingerprint(ev.fingerprint)
    if existing:
        log.info("Application #%s already exists for this job (%s).",
                 existing["id"], existing["status"])
        return int(existing["id"])

    from auto_apply.browser import (
        browser_page, detect_ats, detect_bot_wall, has_saved_session,
        inspect_form, looks_like_application_form, open_application_form,
    )
    from auto_apply.profile_builder import platform_for_url
    from auto_apply.profile_builder import platform_for_source

    board = platform_for_source(ev.source_platform, ev.direct_link)
    session_key = board.slug if board else (ev.source_platform or "default")

    fields: list[Any] = []
    form_ok, form_note = False, "page not inspected"
    ats = ""
    if ev.direct_link:
        try:
            # Inspected through the board's saved login. Signed out, these
            # boards serve a public landing page whose only form is the site
            # search -- which is what `looks_like_application_form` then
            # (correctly) refuses, leaving a draft that can never be submitted.
            with browser_page(session_key) as page:
                page.goto(ev.direct_link, wait_until="domcontentloaded")
                # The description is read from the LANDING page, before any
                # click: that is where the posting's own text lives, and the
                # application view usually replaces it with a bare form.
                if not job_description:
                    job_description = page.inner_text("body")[:6000]
                # Click through to the real form FIRST. On an aggregator the
                # landing page has no application form at all -- only a button
                # -- so inspecting it here finds the site search and the draft
                # is born unsubmittable.
                # The click can open a NEW TAB, so the page to inspect is
                # whatever comes back -- not necessarily the one we started on.
                apply_page, opened, open_note = open_application_form(page)
                # A sign-in wall is not a dead end when the vault holds the
                # credentials for THAT board. Sign in natively and try again.
                apply_page, open_note = _sign_in_and_retry(
                    apply_page, store, open_note
                )
                fields = inspect_form(apply_page)
                ats = detect_ats(apply_page)
                form_ok, form_note = looks_like_application_form(
                    fields, str(getattr(apply_page, "url", "") or ""))
                # An anti-bot holding page has no form on it, so every check
                # above reports its own symptom -- "0 fields", "search widget"
                # -- and none of them names the 403 that caused all of it.
                wall = detect_bot_wall(apply_page)
                if not form_ok and wall:
                    form_ok, form_note = False, (
                        f"blocked by an anti-bot check ({wall!r}) -- this "
                        f"browser is never served the real page. Sign in with "
                        f"your normal Chrome, then run: "
                        f"python main.py --capture-session "
                        f"{board.name if board else session_key}"
                    )
                elif opened:
                    form_note = f"{form_note} ({open_note})"
                elif "never automated" in open_note:
                    # A refusal, not a detection failure. Say which it is:
                    # "no form here" and "this one is LinkedIn's" need
                    # completely different responses from the reader.
                    form_ok, form_note = False, open_note
        except Exception as exc:
            log.warning("Could not inspect the application form: %s", exc)
            form_note = f"inspection failed: {exc}"

    # ADD the likely cause; never replace the observation.
    #
    # "The only form here is the site search" is what was actually SEEN, and it
    # stays -- that is the evidence. "You are not signed in" is the most likely
    # REASON for it, and it is the part that says what to do next. Dropping the
    # first for the second would trade a fact for an inference.
    if (not form_ok and board is not None and board.needs_login
            and not has_saved_session(board.slug)):
        form_note = (
            f"{form_note}. Most likely cause: not signed in to {board.name} -- "
            f"its apply form only exists for a logged-in candidate. "
            f"Run: python main.py --register {board.name}"
        )

    if not form_ok:
        log.warning(
            "No usable application form on %s -- %s. The draft is still saved "
            "so the cover letter is ready, but submission will be refused.",
            ev.direct_link[:70], form_note,
        )

    questions = [f.label for f in fields if f.is_question]
    draft = draft_answers(ev, job_description, questions)
    payload = build_payload(ev, draft, fields)
    experience = _experience_label()

    app_id = store.record_application(
        job_fingerprint=ev.fingerprint, job_id=ev.ref_id,
        company=ev.company_name, role=ev.role_title,
        platform=ev.source_platform, job_url=ev.direct_link,
        payload={
            "fields": payload,
            # What each selector IS, so a later in-line edit can find the right
            # input instead of only rewriting the draft's own copy.
            "field_map": describe_fields(fields),
            "draft": draft,
            "experience": experience,
            "match_score": int(ev.match_score or 0),
            "form_ok": form_ok,
            "form_note": form_note,
            "ats": ats,
            "board": board.name if board else "",
        },
        cover_letter=draft.get("cover_letter", ""),
        status=STATUS_REVIEW if _cfg().get("require_approval", True) else STATUS_APPROVED,
    )

    if notifier:
        from auto_apply.review import DraftCard, dispatch_review

        # BOTH channels, not just Telegram. The whole point of the review gate
        # is that the user answers it; a card that only lands where they are not
        # looking is a gate that stalls rather than one that holds.
        dispatch_review(notifier, DraftCard.from_draft(
            app_id, ev, draft, ev.source_platform,
            experience=experience, form_ok=form_ok, form_note=form_note,
        ))
    log.info("Application #%d drafted and sent for review.", app_id)
    return app_id


def _sign_in_and_retry(page: Any, store: SecureStore,
                       note: str) -> tuple[Any, str]:
    """If we landed on a sign-in wall, log in with the vault and carry on.

    The board is not refusing the application -- it is refusing an anonymous
    visitor, which is a completely different problem and one we hold the answer
    to. Uses the NATIVE email/password form; a "Continue with Google" button
    would hand the flow to an OAuth consent screen that rejects an
    automation-driven browser outright.

    Never raises: if the login does not take, the caller carries on with the
    page it already had and the draft records why.
    """
    from auto_apply.browser import (
        inspect_form, login_with_password, looks_like_application_form,
        open_application_form,
    )
    from auto_apply.profile_builder import platform_for_url

    fields = inspect_form(page)
    ok, why = looks_like_application_form(fields)
    if ok or "sign-in" not in why:
        return page, note

    url = str(getattr(page, "url", "") or "")
    board = platform_for_url(url)
    if board is None:
        return page, f"{note}; hit a sign-in wall on an unrecognised board"

    account = store.get_credentials(board.name)
    if not account or not account.get("password"):
        return page, (f"{note}; {board.name} wants a login and the vault has "
                      f"no credentials for it -- run: python main.py "
                      f"--register {board.name}")

    signed_in, detail = login_with_password(
        page, account.get("email") or "", account.get("password") or ""
    )
    log.info("Sign-in wall on %s: %s", board.name, detail)
    if not signed_in:
        return page, (f"{note}; could not sign in to {board.name} ({detail})")

    # Signed in now, so the apply control may finally lead somewhere.
    page, _opened, retry_note = open_application_form(page)
    return page, f"{note}; signed in to {board.name} and retried ({retry_note})"


def _experience_label() -> str:
    """"3 years", from the structured CV profile. "" if it cannot be read.

    Read here, at drafting time, rather than inside the card renderer: reading
    it lazily would make rendering a review card able to trigger a Gemini CV
    extraction, and re-rendering after every edit would do it again.
    """
    try:
        from auto_apply.candidate import load_candidate

        years = load_candidate().years_experience
    except Exception as exc:
        log.debug("Could not read years of experience from the profile: %s", exc)
        return ""
    if not years:
        return ""
    whole = int(years)
    return f"{whole} year{'' if whole == 1 else 's'}"


def refresh_draft(app_id: int, store: SecureStore,
                  notifier: Any = None) -> tuple[bool, str]:
    """Re-inspect an existing draft's page and update whether it is submittable.

    Deliberately NOT a re-draft. The cover letter and the screening answers are
    the expensive part -- they cost a Gemini call each, and the daily free-tier
    allowance is small enough that regenerating them to fix a form-detection
    problem is a bad trade. Everything the model wrote is kept; only what the
    BROWSER found is refreshed.

    That matters most exactly when it is most needed: a draft written before a
    board login existed can be re-checked afterwards without spending a single
    token, and without the quota being the reason the fix cannot be verified.
    """
    from auto_apply.browser import (
        browser_page, detect_already_applied, detect_ats, detect_bot_wall,
        inspect_form, looks_like_application_form, open_application_form,
    )
    from auto_apply.profile_builder import platform_for_source
    from auto_apply.review import DraftCard, dispatch_review, payload_of

    app = store.get_application(app_id)
    if not app:
        raise ApplyError(f"No application #{app_id}.")
    if app["status"] in (STATUS_SUBMITTED, STATUS_DECLINED):
        return False, f"#{app_id} is {app['status']}; nothing to refresh"

    url = str(app.get("job_url") or "")
    if not url:
        return False, f"#{app_id} has no job URL to re-inspect"

    allowed, reason = is_automatable(app.get("platform", ""))
    if not allowed:
        return False, reason

    board = platform_for_source(app.get("platform", ""), url)
    session_key = board.slug if board else (app.get("platform") or "default")
    payload = payload_of(app)

    was_ok = payload.get("form_ok") is True
    try:
        with browser_page(session_key) as page:
            page.goto(url, wait_until="domcontentloaded")
            apply_page, _opened, open_note = open_application_form(page)
            apply_page, open_note = _sign_in_and_retry(apply_page, store,
                                                       open_note)
            fields = inspect_form(apply_page)
            form_ok, form_note = looks_like_application_form(
                fields, str(getattr(apply_page, "url", "") or ""))
            payload["ats"] = detect_ats(apply_page)
            payload["field_map"] = describe_fields(fields)
            wall = detect_bot_wall(apply_page) if not form_ok else ""
            applied = detect_already_applied(apply_page) if not form_ok else ""
    except Exception as exc:
        return False, f"#{app_id} could not be re-inspected: {exc}"[:200]

    if applied:
        # The board has this application already. Leaving the row pending
        # would keep offering the user a "done 7" that cannot work, and any
        # attempt to satisfy it risks a duplicate application under their name.
        payload["form_note"] = f"the board says {applied!r}"
        store.update_application_draft(app_id, payload=payload)
        store.set_application_status(app_id, STATUS_SUBMITTED)
        return True, (f"#{app_id} ALREADY APPLIED on the board "
                      f"({applied!r}) -- marked submitted, nothing left to do")

    payload["form_ok"] = form_ok
    if wall:
        # Name the 403, not its symptom. Everything above was describing a
        # holding page: "0 fields" and "search widget" are both true of one,
        # and neither tells the reader that the real page was never served.
        payload["form_note"] = (
            f"blocked by an anti-bot check ({wall!r}) -- this browser is never "
            f"served the real page. Sign in with your normal Chrome, then run: "
            f"python main.py --capture-session "
            f"{board.name if board else session_key}"
        )
    else:
        payload["form_note"] = (f"{form_note} ({open_note})" if open_note
                                else form_note)
    store.update_application_draft(app_id, payload=payload)

    changed = form_ok != was_ok
    if changed and notifier is not None:
        # Only re-card the user when the ANSWER changed. A draft that is still
        # blocked for the same reason is not news, and re-sending it on every
        # refresh is how a review channel becomes noise people stop reading.
        dispatch_review(notifier,
                        DraftCard.from_row(store.get_application(app_id)))

    state = "SUBMITTABLE" if form_ok else "still blocked"
    return changed, f"#{app_id} {state}: {payload['form_note'][:120]}"


def submit_application(
    app_id: int, store: SecureStore, notifier: Any, dry_run: bool = False
) -> bool:
    """Execute a previously approved application and capture the evidence."""
    app = store.get_application(app_id)
    if not app:
        raise ApplyError(f"No application #{app_id}.")

    if app["status"] not in (STATUS_APPROVED, STATUS_DRAFT):
        raise ApplyError(
            f"Application #{app_id} is '{app['status']}', not approved. "
            f"Approve it first: python main.py --approve {app_id}"
        )

    allowed, reason = is_automatable(app.get("platform", ""))
    if not allowed:
        raise ApplyError(reason)

    # Refuse BEFORE launching a browser. Drafting already established whether
    # the page has a real application form; a job that has none is not a
    # failure to retry, it is one to apply to by hand. Raising here keeps it
    # alongside the other pre-conditions instead of being recorded as a crash.
    #
    # FAIL CLOSED: only an explicit True may proceed. This was `is False`,
    # which let two cases straight through to the browser -- a draft written
    # before `form_ok` was recorded at all, and one whose payload is empty.
    # Both read as "unknown", and the first is not hypothetical: the pending
    # draft on this machine had form_ok=None and its only detected fields were
    # `keywords` and `state`, i.e. Tanqeeb's SEARCH BOX. Submitting that runs a
    # search and reports it as a delivered application, which is worse than
    # failing because it looks like success.
    stored = json.loads(app.get("submitted_payload_json") or "{}")
    if stored.get("form_ok") is not True:
        note = str(stored.get("form_note") or
                   "this draft predates form detection, so the page was never "
                   "checked for a real application form")
        raise ApplyError(
            "No confirmed application form on this page (" + note
            + "). The drafted cover letter is saved -- apply by hand: "
            + str(app.get("job_url") or "")
            + "\nRe-draft to re-inspect the page: python main.py --apply"
        )

    from auto_apply.browser import (
        MULTI_STEP_ATS, attach_cv, browser_page, capture_evidence, click_next,
        click_submit, detect_ats, detect_captcha, fill_field, has_submit,
        inspect_form, looks_like_application_form, open_application_form,
    )
    from auto_apply.profile_builder import platform_for_source

    payload = json.loads(app.get("submitted_payload_json") or "{}")
    field_values: dict[str, str] = payload.get("fields", {})
    cv_path = next((str(p) for p in settings.cv_paths if p.exists()), "")
    board = platform_for_source(app.get("platform", ""), app.get("job_url", ""))
    session_key = board.slug if board else (app.get("platform") or "default")

    if dry_run:
        log.info("[DRY_RUN] would submit #%d with %d field(s) to %s",
                 app_id, len(field_values), app.get("job_url"))
        return True

    try:
        # Opened with the board's SAVED LOGIN. Without it the browser arrives
        # signed out, the board serves its public landing page, and the only
        # form on it is the site search -- which is why every draft was refused
        # at this gate before session state existed.
        with browser_page(session_key) as page:
            page.goto(app["job_url"], wait_until="domcontentloaded")
            # Submission starts from the JOB url, not from wherever drafting
            # ended up, so the same click has to happen again. Without it the
            # submit-time re-check inspects the landing page and refuses a
            # draft that was perfectly valid.
            apply_page, opened, open_note = open_application_form(page)
            if opened:
                log.info("Application view opened: %s", open_note)
            elif "never automated" in open_note:
                raise ApplyError(
                    "Refusing to submit: " + open_note
                    + ". Apply by hand here: " + str(app["job_url"])
                )
            # Everything from here works on the page the apply flow actually
            # landed on, which may be a new tab on the employer's own site.
            page = apply_page
            fields = inspect_form(page)
            ats = detect_ats(page)
            if ats:
                log.info("Application is served by %s.", ats)

            # Re-check at submit time, not just at draft time: the page may have
            # changed, and clicking "submit" on a search widget would run a
            # search and report it as a submitted application.
            usable, note = looks_like_application_form(fields)
            if not usable:
                raise ApplyError(
                    "Refusing to submit: " + note
                    + ". Apply by hand here: " + str(app["job_url"])
                )

            # A CAPTCHA is the same failure shape as a search widget: clicking
            # submit underneath an unsolved challenge does not raise, the page
            # re-renders with an error, and the screenshot of that error page
            # gets banked as evidence of a submission that never happened.
            # Refuse instead, and say what is in the way.
            blocked, challenge = detect_captcha(page)
            if blocked:
                raise ApplyError(
                    f"Refusing to submit: this page is behind {challenge}, which "
                    f"only a human can clear. The drafted cover letter is saved "
                    f"-- apply by hand here: {app['job_url']}"
                )

            # A CV upload with no CV on disk is not a partial success. The
            # board rejects the submission, but we would still click submit,
            # capture a screenshot of the error page and record it as
            # 'submitted' -- the same looks-like-success failure mode as
            # filling a search widget.
            resume_fields = [f for f in fields if f.kind == "resume"]
            if resume_fields and not cv_path:
                tried = ", ".join(str(p) for p in settings.cv_paths) or "(none configured)"
                if any(f.required for f in resume_fields):
                    raise ApplyError(
                        "This form requires a CV upload and no CV file exists. "
                        "Looked in: " + tried
                        + "\nSet CV_PATH in .env or drop the PDF at one of the "
                        "paths above, then re-run: python main.py --approve "
                        + str(app_id)
                    )
                log.warning(
                    "The form has a CV field but no CV file was found (looked "
                    "in: %s); submitting without an attachment.", tried,
                )

            def fill_visible(page_fields: list[Any]) -> int:
                count = 0
                for f in page_fields:
                    if f.kind == "resume":
                        count += int(attach_cv(page, f, cv_path))
                    elif f.selector in field_values:
                        count += int(fill_field(page, f, field_values[f.selector]))
                return count

            filled = fill_visible(fields)
            log.info("Filled %d/%d field(s) on the first page.",
                     filled, len(fields))

            # MULTI-STEP FORMS. Workday, Taleo, iCIMS and SmartRecruiters are
            # wizards: the submit button does not exist until the last page.
            # Walk forward, filling whatever each page exposes, and stop the
            # moment a real submit control appears. Bounded, because a wizard
            # that loops -- a validation error re-rendering the same step -- is
            # otherwise an infinite click loop.
            max_steps = int(_cfg().get("max_form_steps", 6))
            steps = 1
            while not has_submit(page) and steps < max_steps:
                if not click_next(page):
                    break
                steps += 1
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                page_fields = inspect_form(page)
                filled += fill_visible(page_fields)
                blocked, challenge = detect_captcha(page)
                if blocked:
                    raise ApplyError(
                        f"Refusing to submit: step {steps} of this form is "
                        f"behind {challenge}, which only a human can clear. "
                        f"Apply by hand here: {app['job_url']}"
                    )
            if steps > 1:
                log.info("Walked %d step(s) of a %s form; %d field(s) filled "
                         "in total.", steps, ats or "multi-page", filled)
            if ats in MULTI_STEP_ATS and steps == 1:
                log.info("%s is usually multi-step but the submit control was "
                         "already present; submitting this page.", ats)

            if not click_submit(page):
                raise ApplyError(
                    "Could not find a submit button on the form"
                    + (f" after {steps} step(s)" if steps > 1 else "")
                    + "."
                )

            page.wait_for_load_state("networkidle", timeout=30000)
            shot = capture_evidence(page, f"app{app_id}_{app.get('company','')}")

        store.set_application_status(app_id, STATUS_SUBMITTED, screenshot_path=shot)
        if notifier:
            from auto_apply.review import dispatch_submitted

            # Both channels, and the screenshot itself rides the Telegram side.
            # "Submitted" is a claim; the full-page capture is the proof, and
            # the proof is the part worth pushing to the user's phone.
            dispatch_submitted(notifier, app_id, store.get_application(app_id))
        return True

    except Exception as exc:
        store.set_application_status(
            app_id, STATUS_FAILED, failure_reason=f"{type(exc).__name__}: {exc}"[:300]
        )
        log.error("Application #%d failed: %s", app_id, exc)
        if notifier:
            from auto_apply.review import dispatch_failure

            dispatch_failure(notifier, app_id, app, f"{type(exc).__name__}: {exc}")
        return False


def approve(app_id: int, store: SecureStore) -> bool:
    app = store.get_application(app_id)
    if not app:
        raise ApplyError(f"No application #{app_id}.")
    store.set_application_status(app_id, STATUS_APPROVED)
    log.info("Application #%d approved for submission.", app_id)
    return True


def decline(app_id: int, store: SecureStore) -> bool:
    if not store.get_application(app_id):
        raise ApplyError(f"No application #{app_id}.")
    store.set_application_status(app_id, STATUS_DECLINED)
    log.info("Application #%d declined; it will not be submitted.", app_id)
    return True
