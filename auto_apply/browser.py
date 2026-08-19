"""
Playwright plumbing shared by the registration and application flows.

The interesting part is `inspect_form`. Every job board names its inputs
differently -- `applicant_phone`, `txtMobile`, `candidate[phone_number]` -- so
matching on any one site's markup is useless. Instead each field is scored
against the text a HUMAN would read to understand it: its label, placeholder,
aria-label, name and id, combined. That degrades gracefully: an unrecognised
field is reported as `unknown` with its question text intact, and the engine
sends it to Gemini rather than guessing or silently skipping it.

Sessions are persistent per platform, so a login survives between runs and the
bot is not repeatedly hammering sign-in forms.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config import ROOT, settings

log = logging.getLogger(__name__)

SCREENSHOT_DIR = ROOT / "screenshots"
SESSION_DIR = ROOT / "state" / "browser_sessions"

# Semantic field -> substrings that identify it, most specific first.
FIELD_PATTERNS: dict[str, tuple[str, ...]] = {
    "email":            ("e-mail", "email", "بريد"),
    "phone":            ("mobile", "phone", "tel", "whatsapp", "هاتف", "جوال", "موبايل"),
    "first_name":       ("first name", "given name", "الاسم الاول"),
    "last_name":        ("last name", "surname", "family name", "اسم العائلة"),
    "full_name":        ("full name", "your name", "candidate name", "الاسم"),
    "password":         ("password", "كلمة المرور", "كلمة السر"),
    "salary":           ("salary", "expected pay", "compensation", "expected ctc",
                         "الراتب", "الأجر"),
    "notice_period":    ("notice period", "availability", "start date", "متى يمكنك"),
    "years_experience": ("years of experience", "experience years", "سنوات الخبرة"),
    "current_title":    ("current title", "job title", "current position",
                         "المسمى الوظيفي"),
    "current_employer": ("current company", "current employer", "employer",
                         "الشركة الحالية"),
    "location":         ("location", "city", "address", "المدينة", "الموقع"),
    "linkedin":         ("linkedin",),
    "cover_letter":     ("cover letter", "motivation", "why do you", "message",
                         "additional information", "خطاب", "لماذا"),
    "resume":           ("resume", "cv", "curriculum", "attach", "upload",
                         "السيرة الذاتية"),
}

# Inputs that must never be auto-filled.
_SKIP_TYPES = {"hidden", "submit", "button", "image", "reset"}


@dataclass
class FormField:
    """One input, described the way a person would describe it."""

    selector: str
    kind: str                      # semantic name, or "unknown"
    input_type: str                # text / textarea / select / file / checkbox
    label: str                     # the question as rendered
    required: bool = False
    options: list[str] = field(default_factory=list)

    @property
    def is_question(self) -> bool:
        """A free-text field we have no canned answer for -- Gemini's job."""
        return self.kind in ("unknown", "cover_letter") and self.input_type in (
            "text", "textarea"
        )


def _classify(haystack: str) -> str:
    blob = re.sub(r"\s+", " ", (haystack or "").lower())
    if not blob:
        return "unknown"
    # Longest pattern first, so "first name" beats "name".
    best, best_len = "unknown", 0
    for kind, needles in FIELD_PATTERNS.items():
        for needle in needles:
            if needle in blob and len(needle) > best_len:
                best, best_len = kind, len(needle)
    return best


def timestamped_screenshot_path(prefix: str) -> Path:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", prefix)[:48]
    return SCREENSHOT_DIR / f"{stamp}_{safe}.png"


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


@contextmanager
def browser_page(
    platform: str = "default", headed: bool | None = None
) -> Iterator[Any]:
    """Yield a Playwright page with a persistent per-platform session.

    Persistence matters: a login survives between runs, so the bot signs in
    once rather than repeatedly submitting credentials -- which is both slower
    and exactly the pattern that trips anti-automation heuristics.
    """
    if not playwright_available():
        raise RuntimeError(
            "Playwright is not installed. Run:\n"
            "  pip install -r requirements.txt\n"
            "  python -m playwright install chromium"
        )

    from playwright.sync_api import sync_playwright

    cfg = settings.raw.get("auto_apply", {}) or {}
    show = cfg.get("headed", True) if headed is None else headed
    profile_dir = SESSION_DIR / re.sub(r"[^A-Za-z0-9_-]+", "_", platform.lower())
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=not show,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="Africa/Cairo",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context.set_default_timeout(int(cfg.get("page_timeout", 45)) * 1000)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            yield page
        finally:
            try:
                context.close()
            except Exception:
                pass


def _label_for(page: Any, handle: Any) -> str:
    """Everything a reader would use to understand this input."""
    parts: list[str] = []
    for attr in ("aria-label", "placeholder", "name", "id", "title"):
        try:
            value = handle.get_attribute(attr)
        except Exception:
            value = None
        if value:
            parts.append(value)

    # <label for=id>, then any ancestor <label>, then the preceding text node.
    try:
        field_id = handle.get_attribute("id")
        if field_id:
            lab = page.query_selector(f'label[for="{field_id}"]')
            if lab:
                parts.append(lab.inner_text())
        if not parts:
            lab = handle.evaluate_handle("e => e.closest('label')")
            text = lab.evaluate("e => e ? e.innerText : ''") if lab else ""
            if text:
                parts.append(text)
    except Exception:
        pass
    return " | ".join(p.strip() for p in parts if p and p.strip())[:300]


def inspect_form(page: Any, root_selector: str = "form") -> list[FormField]:
    """Describe every fillable field on the page.

    Returns semantic descriptions, not selectors alone, so the caller can decide
    what it knows how to answer and what needs the AI.
    """
    fields: list[FormField] = []
    seen: set[str] = set()

    containers = page.query_selector_all(root_selector) or [page]
    for index, container in enumerate(containers):
        try:
            handles = container.query_selector_all(
                "input, textarea, select"
            )
        except Exception:
            continue

        for pos, handle in enumerate(handles):
            try:
                tag = handle.evaluate("e => e.tagName.toLowerCase()")
                itype = (handle.get_attribute("type") or "text").lower()
                if tag == "input" and itype in _SKIP_TYPES:
                    continue

                label = _label_for(page, handle)
                kind = _classify(label)
                name = handle.get_attribute("name") or handle.get_attribute("id") or ""
                key = f"{tag}:{name}:{label[:40]}"
                if key in seen:
                    continue
                seen.add(key)

                if tag == "select":
                    input_type = "select"
                    options = [
                        (o.inner_text() or "").strip()
                        for o in handle.query_selector_all("option")
                    ][:40]
                elif tag == "textarea":
                    input_type, options = "textarea", []
                elif itype == "file":
                    input_type, options = "file", []
                elif itype in ("checkbox", "radio"):
                    input_type, options = itype, []
                else:
                    input_type, options = "text", []

                if itype == "file" or kind == "resume":
                    kind, input_type = "resume", "file"

                selector = (
                    f'{root_selector} >> nth={index} >> {tag} >> nth={pos}'
                    if containers and containers[0] is not page
                    else f"{tag} >> nth={pos}"
                )
                if name:
                    selector = f'[name="{name}"]' if handle.get_attribute("name") \
                        else f"#{name}"

                fields.append(FormField(
                    selector=selector, kind=kind, input_type=input_type,
                    label=label or f"(unlabelled {tag})",
                    required=bool(handle.get_attribute("required")),
                    options=options,
                ))
            except Exception as exc:
                log.debug("Skipping an uninspectable field: %s", exc)
                continue

    log.info("Form inspection found %d field(s): %s", len(fields),
             ", ".join(sorted({f.kind for f in fields})))
    return fields


# Field names that mean "this is the site's search widget", not an apply form.
_SEARCH_FIELD_HINTS = (
    "keyword", "search", "query", "q=", "state", "province", "category",
    "sort", "filter", "newsletter", "subscribe", "login", "sign in",
)


def looks_like_application_form(fields: list[FormField]) -> tuple[bool, str]:
    """Decide whether these fields are really an application form.

    Job pages are full of forms that are not the one we want -- the site's
    search box, a newsletter signup, a login widget. `inspect_form` happily
    describes those, and filling one then clicking "submit" would run a SEARCH
    and report it as a submitted application. That is a worse outcome than
    failing, because it looks like success.

    An application form is identified by evidence a search box never has: a CV
    upload, a cover-letter box, or a cluster of personal-detail inputs.
    """
    kinds = {f.kind for f in fields}
    strong = kinds & {"resume", "cover_letter", "salary", "notice_period"}
    personal = kinds & {"email", "phone", "first_name", "last_name", "full_name"}

    if strong:
        return True, f"found {', '.join(sorted(strong))}"
    if len(personal) >= 2:
        return True, f"found personal fields: {', '.join(sorted(personal))}"

    searchy = [
        f for f in fields
        if any(h in f.selector.lower() or h in f.label.lower()
               for h in _SEARCH_FIELD_HINTS)
    ]
    if searchy and not personal:
        return False, (
            "this looks like the site's search/filter widget, not an "
            "application form (" +
            ", ".join(sorted({f.kind for f in searchy}))[:60] + ")"
        )
    return False, (
        f"no application markers found among {len(fields)} field(s); the apply "
        f"form is probably behind a login or on the employer's own site"
    )


def fill_field(page: Any, field_: FormField, value: str) -> bool:
    """Fill one field. Returns False rather than raising on any failure."""
    if not value:
        return False
    try:
        if field_.input_type == "select":
            page.select_option(field_.selector, label=value, timeout=8000)
        elif field_.input_type == "file":
            page.set_input_files(field_.selector, value, timeout=15000)
        elif field_.input_type in ("checkbox", "radio"):
            page.check(field_.selector, timeout=8000)
        else:
            page.fill(field_.selector, value, timeout=8000)
        return True
    except Exception as exc:
        log.debug("Could not fill %s (%s): %s", field_.kind, field_.selector, exc)
        return False


def capture_evidence(page: Any, prefix: str) -> str:
    """Full-page screenshot. Returns the path, or "" if it could not be taken."""
    path = timestamped_screenshot_path(prefix)
    try:
        page.screenshot(path=str(path), full_page=True)
        log.info("Evidence captured: %s", path)
        return str(path)
    except Exception as exc:
        log.warning("Screenshot failed: %s", exc)
        return ""
