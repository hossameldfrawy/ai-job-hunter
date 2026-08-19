"""
Human-in-the-loop application engine.

    draft  ->  review dispatch  ->  YOUR approval  ->  submit  ->  evidence

The approval step is a real gate, not a notification. Gemini writes the cover
letter and the screening answers; those go to Telegram and the application sits
at `review_pending` until you approve it by id. Nothing is ever submitted in
your name on the strength of a model's guess about your salary expectations.

LinkedIn is excluded at the top of every entry point, not filtered somewhere
downstream, so no future refactor can quietly start automating it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from config import ROOT, settings
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
def draft_answers(
    ev: Evaluation, job_description: str, questions: list[str]
) -> dict[str, Any]:
    """Ask Gemini for a cover letter and one answer per screening question."""
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
            "maxOutputTokens": 3072,
        },
    })
    draft = evaluator._extract_json(payload)        # noqa: SLF001
    draft["_model"] = model
    return draft


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
            payload[f.selector] = answers.get(f.label.strip(), "")
    return {k: v for k, v in payload.items() if v}


# ---------------------------------------------------------------------------
# Review dispatch
# ---------------------------------------------------------------------------
def format_review_message(
    app_id: int, ev: Evaluation, draft: dict[str, Any], platform: str
) -> str:
    answers = draft.get("answers", []) or []
    lines = []
    for a in answers[:6]:
        if not isinstance(a, dict):
            continue
        flag = "" if a.get("confident", True) else "  ⚠️ NEEDS YOU"
        lines.append(f"• {str(a.get('question',''))[:70]}{flag}\n   → "
                     f"{str(a.get('answer',''))[:160]}")
    answers_summary = "\n".join(lines) or "(no screening questions on this form)"

    salary = draft.get("salary_expectation") or ""
    cover = (draft.get("cover_letter") or "")[:600]

    return (
        f"📝 *APPLICATION DRAFT READY FOR REVIEW* [#{app_id}]\n"
        f"🏢 Company: {ev.company_name}\n"
        f"💼 Role: {ev.role_title}\n"
        f"🌐 Platform: {platform}\n"
        f"📊 Match: {ev.match_score}%\n"
        + (f"💰 Salary answer: {salary}\n" if salary else "")
        + f"\n📋 Proposed Answers:\n{answers_summary}\n"
        f"\n✍️ Drafted Cover Letter:\n{cover}\n"
        f"\n──────────────\n"
        f"Approve:  python main.py --approve {app_id}\n"
        f"Discard:  python main.py --decline {app_id}"
    )


def format_submitted_message(app_id: int, app: dict[str, Any]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    shot = app.get("screenshot_path") or ""
    return (
        f"🚀 *APPLICATION SUCCESSFULLY SUBMITTED!* [#{app_id}]\n"
        f"🏢 Company: {app.get('company')}\n"
        f"💼 Role: {app.get('role')}\n"
        f"🌐 Platform: {app.get('platform')}\n"
        f"📸 Evidence: {'Screenshot saved — ' + shot.split(chr(92))[-1] if shot else 'not captured'}\n"
        f"🕒 Time: {stamp}"
    )


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
        browser_page, inspect_form, looks_like_application_form,
    )

    fields: list[Any] = []
    form_ok, form_note = False, "page not inspected"
    if ev.direct_link:
        try:
            with browser_page(ev.source_platform) as page:
                page.goto(ev.direct_link, wait_until="domcontentloaded")
                fields = inspect_form(page)
                form_ok, form_note = looks_like_application_form(fields)
                if not job_description:
                    job_description = page.inner_text("body")[:6000]
        except Exception as exc:
            log.warning("Could not inspect the application form: %s", exc)
            form_note = f"inspection failed: {exc}"

    if not form_ok:
        log.warning(
            "No usable application form on %s -- %s. The draft is still saved "
            "so the cover letter is ready, but submission will be refused.",
            ev.direct_link[:70], form_note,
        )

    questions = [f.label for f in fields if f.is_question]
    draft = draft_answers(ev, job_description, questions)
    payload = build_payload(ev, draft, fields)

    app_id = store.record_application(
        job_fingerprint=ev.fingerprint, job_id=ev.ref_id,
        company=ev.company_name, role=ev.role_title,
        platform=ev.source_platform, job_url=ev.direct_link,
        payload={"fields": payload, "draft": draft,
                 "form_ok": form_ok, "form_note": form_note},
        cover_letter=draft.get("cover_letter", ""),
        status=STATUS_REVIEW if _cfg().get("require_approval", True) else STATUS_APPROVED,
    )

    if notifier:
        message = format_review_message(app_id, ev, draft, ev.source_platform)
        if not form_ok:
            message += (
                "\n\n⚠️ *No auto-submittable form found* — "
                + form_note
                + "\nThe cover letter above is ready to paste. Apply here:\n"
                + (ev.direct_link or "(no link)")
            )
        notifier.send_via_telegram(message)
    log.info("Application #%d drafted and sent for review.", app_id)
    return app_id


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

    # Refuse before launching a browser. Drafting already established whether
    # the page has a real application form; a job that has none is not a
    # failure to retry, it is one to apply to by hand. Raising here keeps it
    # alongside the other pre-conditions instead of being recorded as a crash.
    stored = json.loads(app.get("submitted_payload_json") or "{}")
    if stored and stored.get("form_ok") is False:
        raise ApplyError(
            "No auto-submittable application form on this page ("
            + str(stored.get("form_note", "unknown"))
            + "). The drafted cover letter is saved -- apply by hand: "
            + str(app.get("job_url") or "")
        )

    from auto_apply.browser import (
        browser_page, capture_evidence, fill_field, inspect_form,
        looks_like_application_form,
    )

    payload = json.loads(app.get("submitted_payload_json") or "{}")
    field_values: dict[str, str] = payload.get("fields", {})
    cv_path = next((str(p) for p in settings.cv_paths if p.exists()), "")

    if dry_run:
        log.info("[DRY_RUN] would submit #%d with %d field(s) to %s",
                 app_id, len(field_values), app.get("job_url"))
        return True

    try:
        with browser_page(app.get("platform", "default")) as page:
            page.goto(app["job_url"], wait_until="domcontentloaded")
            fields = inspect_form(page)

            # Re-check at submit time, not just at draft time: the page may have
            # changed, and clicking "submit" on a search widget would run a
            # search and report it as a submitted application.
            usable, note = looks_like_application_form(fields)
            if not usable:
                raise ApplyError(
                    "Refusing to submit: " + note
                    + ". Apply by hand here: " + str(app["job_url"])
                )

            filled = 0
            for f in fields:
                if f.kind == "resume" and cv_path:
                    filled += int(fill_field(page, f, cv_path))
                elif f.selector in field_values:
                    filled += int(fill_field(page, f, field_values[f.selector]))
            log.info("Filled %d/%d field(s).", filled, len(fields))

            submitted = False
            for selector in (
                'button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Apply")', 'button:has-text("Submit")',
                'button:has-text("تقديم")', 'button:has-text("إرسال")',
            ):
                try:
                    page.click(selector, timeout=5000)
                    submitted = True
                    break
                except Exception:
                    continue
            if not submitted:
                raise ApplyError("Could not find a submit button on the form.")

            page.wait_for_load_state("networkidle", timeout=30000)
            shot = capture_evidence(page, f"app{app_id}_{app.get('company','')}")

        store.set_application_status(app_id, STATUS_SUBMITTED, screenshot_path=shot)
        if notifier:
            notifier.send_via_telegram(
                format_submitted_message(app_id, store.get_application(app_id))
            )
        return True

    except Exception as exc:
        store.set_application_status(
            app_id, STATUS_FAILED, failure_reason=f"{type(exc).__name__}: {exc}"[:300]
        )
        log.error("Application #%d failed: %s", app_id, exc)
        if notifier:
            notifier.send_via_telegram(
                f"⚠️ *APPLICATION #{app_id} FAILED*\n"
                f"🏢 {app.get('company')} — {app.get('role')}\n"
                f"Reason: {str(exc)[:200]}\n\n"
                f"The draft is kept; apply manually here:\n{app.get('job_url')}"
            )
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
