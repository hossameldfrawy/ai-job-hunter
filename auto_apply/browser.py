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

# Semantic field -> substrings that identify it.
#
# ORDER IS SIGNIFICANT: the groups run most-specific to most-generic, and
# `_classify` returns the FIRST group that matches. See its docstring for why
# that beats picking the longest matching substring.
FIELD_PATTERNS: dict[str, tuple[str, ...]] = {
    # Before "email" on purpose: several boards label the sign-in handle
    # "username or email", and that field wants the handle, not the address.
    "username":         ("username", "user name", "login id", "handle",
                         "screen name", "اسم المستخدم"),
    "email":            ("e-mail", "email", "بريد"),
    "phone":            ("mobile", "phone", "tel", "whatsapp", "هاتف", "جوال", "موبايل"),
    "first_name":       ("first name", "given name", "الاسم الاول"),
    "last_name":        ("last name", "surname", "family name", "اسم العائلة"),
    "full_name":        ("full name", "your name", "candidate name", "الاسم"),
    "password":         ("password", "كلمة المرور", "كلمة السر"),
    "salary":           ("salary", "expected pay", "compensation", "expected ctc",
                         "الراتب", "الأجر"),
    "notice_period":    ("notice period", "availability", "start date", "متى يمكنك"),
    # Deliberately NOT a bare "experience": "Describe your experience with
    # Asterisk" is a screening question for the model to answer, and matching
    # it here would type the number 3 into a free-text box.
    "years_experience": ("years of experience", "years experience",
                         "yearsexperience", "experience years",
                         "total experience", "yrs experience",
                         "experience in years", "سنوات الخبرة"),
    "current_title":    ("current title", "job title", "current position",
                         "المسمى الوظيفي"),
    "current_employer": ("current company", "current employer", "employer",
                         "الشركة الحالية"),
    "location":         ("location", "city", "address", "المدينة", "الموقع"),
    "linkedin":         ("linkedin",),
    # Profile-building fields. These only appear on REGISTRATION forms, and
    # they are the ones that make a profile look finished to a recruiter --
    # leaving them blank is the difference between a real profile and a stub.
    # Both sit ahead of `cover_letter` because "about you" and "professional
    # summary" would otherwise be swallowed by its "additional information".
    # No "job title" needle here: `current_title` owns that, and it runs first.
    "headline":         ("headline", "professional title", "profile title",
                         "tagline", "المسمى المهني"),
    # NOT "about you": it matches inside "Tell us about your most difficult
    # outage", which is a screening question for the model to answer, and
    # filling it with the CV summary answers a question nobody asked.
    "bio":              ("bio", "biography", "about yourself",
                         "professional summary", "career summary", "objective",
                         "personal statement", "نبذة", "الملخص المهني"),
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


# Punctuation that separates words inside a form's own naming, rather than
# carrying meaning. `candidate[cover_letter]`, `applicant-email` and
# `notice.period` are all ordinary conventions for a `name` attribute, and
# `_label_for` feeds those attributes straight in -- so without folding them to
# spaces, the space-separated patterns above match none of them. That is not a
# corner case: snake_case IS the convention for HTML name attributes, so a form
# whose inputs carry no visible <label> was classified almost entirely as
# `unknown` and every field on it went to Gemini as a free-text question.
_LABEL_SEPARATORS = re.compile(r"[\s_\-\[\]().:;,/\\|]+")


def _fold_label(text: str | None) -> str:
    return _LABEL_SEPARATORS.sub(" ", (text or "").lower()).strip()


#: Below this length, a single Latin word is matched as a WHOLE TOKEN rather
#: than as a substring. Substring matching is what lets `emailAddress` (which
#: folds to one word) resolve at all, but on a short needle it is a liability:
#: "tel" matches inside "Tell us about your most difficult outage", and "city"
#: matches inside "capacity". Both were live misclassifications -- the first
#: put the candidate's phone number into a free-text essay box.
_SHORT_NEEDLE = 4


def _token_only(needle: str) -> bool:
    return (len(needle) <= _SHORT_NEEDLE
            and needle.isascii() and needle.isalpha())


#: The patterns, folded the same way the label is, longest first within each
#: group, each paired with how it must be matched. Precomputed because
#: `inspect_form` calls `_classify` once per input on the page.
_FOLDED_PATTERNS: dict[str, tuple[tuple[str, bool], ...]] = {
    kind: tuple(
        (needle, _token_only(needle))
        for needle in sorted(
            {_fold_label(n) for n in needles if _fold_label(n)},
            key=len, reverse=True,
        )
    )
    for kind, needles in FIELD_PATTERNS.items()
}


def _squash(text: str) -> str:
    """Drop every separator: "first name" and "firstname" become one string."""
    return re.sub(r"[^a-z0-9؀-ۿ]+", "", (text or "").lower())


#: Multi-word patterns with their separators removed.
#:
#: Wuzzuf names its inputs `firstname`, `lastname` -- one word, no separator at
#: all -- so the space-separated patterns matched none of them and a perfectly
#: ordinary signup form came back as five `unknown` fields. Only MULTI-WORD
#: needles are squashed: a single short word like "cv" or "tel" is already
#: risky as a substring, and removing word boundaries entirely would make it
#: far worse.
_SQUASHED_PATTERNS: dict[str, tuple[str, ...]] = {
    kind: tuple(sorted(
        {
            _squash(needle)
            for needle, _token in pairs
            if " " in needle and len(_squash(needle)) >= 7
        },
        key=len, reverse=True,
    ))
    for kind, pairs in _FOLDED_PATTERNS.items()
}


def _classify(haystack: str) -> str:
    """Name the semantic field this label describes, or "unknown".

    Matching is by GROUP PRIORITY, not by longest substring. Longest-substring
    was the obvious rule and it is wrong in a way that costs real data: the
    label "Email address" contains both "email" (5) and location's "address"
    (7), so the longer one won and the form got the candidate's city typed into
    the email box. Priority ordering has no such failure -- a label is
    classified by the most specific concept it mentions, and the table is
    written most-specific-first for exactly that reason.

    Within one group the needles are tried longest-first, which is what keeps
    "الاسم الاول" from being read as the bare "الاسم".
    """
    blob = _fold_label(haystack)
    if not blob:
        return "unknown"
    padded = f" {blob} "
    squashed = _squash(blob)
    for kind, needles in _FOLDED_PATTERNS.items():
        for needle, whole_token in needles:
            if f" {needle} " in padded if whole_token else needle in blob:
                return kind
        # Then the same patterns with their separators removed, which is how
        # boards actually name inputs: `firstname`, `coverletter`, `jobtitle`.
        if any(needle in squashed for needle in _SQUASHED_PATTERNS[kind]):
            return kind
    return "unknown"


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


def platform_slug(platform: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(platform or "default").lower()).strip("_") \
        or "default"


def storage_state_path(platform: str) -> Path:
    """Where this board's authenticated cookies live.

    A separate, portable artefact from the Chromium profile directory. The
    profile already persists a login on THIS machine; the state file is the
    part you can inspect, back up, delete when a board logs you out, and copy
    to another machine. Both are secrets -- they are a logged-in session -- so
    they live under `secrets/` and are git-ignored like the vault.
    """
    configured = (settings.raw.get("auto_apply", {}) or {}).get("session_dir")
    base = Path(configured) if configured else Path("secrets/sessions")
    if not base.is_absolute():
        base = ROOT / base
    return base / f"{platform_slug(platform)}_state.json"


def has_saved_session(platform: str) -> bool:
    """Is there a session file at all? Cheap, and deliberately shallow.

    `session_status` is the one to ask whether it actually signs you IN.
    """
    path = storage_state_path(platform)
    try:
        return path.exists() and path.stat().st_size > 2
    except OSError:
        return False


#: Cookie names that indicate an authenticated session rather than analytics.
#:
#: Needed because "a session file exists" is nearly worthless as a signal. A
#: browser that merely LOADED a board's landing page saves dozens of cookies --
#: Bing, Clarity, DoubleClick, AppNexus -- and a file full of those looks
#: exactly like a successful login by size. Measured on this machine: the
#: Talent.com state held six cookies, every one of them analytics
#: (`NEXT_LOCALE`, `statsig.stable_id`, `utm_source`), while Tanqeeb held
#: `token` and `user_id` and Wuzzuf held `LiToken` and `ci_sessions`. Reporting
#: all three as "signed in" is the false confidence that sends someone hunting
#: for a form bug that is really a missing login.
#:
#: Bare "uid" is deliberately absent: it matches inside `uet_nuuid`, which is a
#: Microsoft advertising cookie.
AUTH_COOKIE_HINTS: tuple[str, ...] = (
    "token", "session", "sess", "auth", "login", "user_id", "userid",
    "remember", "jwt", "identity", "passport", "credential",
)


def _own_cookies(cookies: list[dict[str, Any]],
                 hosts: tuple[str, ...]) -> list[dict[str, Any]]:
    """Cookies set by the BOARD itself, not by a tracker embedded on it."""
    if not hosts:
        return list(cookies)
    out = []
    for cookie in cookies:
        domain = str(cookie.get("domain", "")).lstrip(".").lower()
        if any(domain == h or domain.endswith("." + h) for h in hosts):
            out.append(cookie)
    return out


def session_status(platform: str,
                   hosts: tuple[str, ...] = ()) -> tuple[bool, str]:
    """Does this saved session actually sign us in? (ok, human explanation).

    Never raises: a status panel that crashes on a malformed file is worse than
    one that says the file is malformed.
    """
    import json as _json
    import time as _time

    path = storage_state_path(platform)
    if not path.exists():
        return False, "no saved login"
    try:
        state = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False, "state file unreadable — re-run --register"

    cookies = [c for c in (state.get("cookies") or []) if isinstance(c, dict)]
    if not cookies:
        return False, "file has no cookies — re-run --register"

    own = _own_cookies(cookies, hosts)
    if not own:
        return False, f"{len(cookies)} third-party cookies only — NOT signed in"

    now = _time.time()

    def live(cookie: dict[str, Any]) -> bool:
        expires = cookie.get("expires")
        try:
            expires = float(expires)
        except (TypeError, ValueError):
            return True                     # no expiry recorded: session cookie
        return expires < 0 or expires > now

    auth = [c for c in own
            if any(hint in str(c.get("name", "")).lower()
                   for hint in AUTH_COOKIE_HINTS)]
    if not auth:
        return False, (f"{len(own)} cookies but no auth token — NOT signed in")
    if not any(live(c) for c in auth):
        return False, "session expired — re-run --register"

    # "auth cookies present", NOT "signed in".
    #
    # This is an INFERENCE from cookie names, and it can be wrong: Tanqeeb's
    # saved state carries `token` and `user_id` on its own domain, and clicking
    # Apply still lands on /users/login. A cookie that exists is not a cookie
    # the server still honours. The authoritative check is what the board does
    # when you try to apply -- which `looks_like_application_form` now reports
    # as a sign-in wall -- so this line must not out-confidence it.
    names = ", ".join(sorted({str(c.get("name")) for c in auth})[:3])
    return True, f"auth cookies present ({len(own)}: {names})"


def save_storage_state(context: Any, platform: str) -> str:
    """Bank the authenticated cookies. Returns the path, or "" on failure.

    Called after YOU have signed in (or solved the CAPTCHA and submitted), so
    the next `--apply` opens the board's pages as a logged-in candidate and
    sees the real application form instead of the public landing page.
    """
    path = storage_state_path(platform)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        gitignore = path.parent / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n", encoding="utf-8")
        context.storage_state(path=str(path))
        try:                                    # best effort; POSIX only
            path.chmod(0o600)
        except OSError:
            pass
        log.info("Saved the authenticated %s session to %s", platform, path)
        return str(path)
    except Exception as exc:
        log.warning("Could not save the %s session: %s", platform, exc)
        return ""


def clear_storage_state(platform: str) -> bool:
    path = storage_state_path(platform)
    if path.exists():
        path.unlink()
        log.info("Cleared the saved %s session.", platform)
        return True
    return False


@contextmanager
def browser_context(
    platform: str = "default", headed: bool | None = None,
    use_state: bool = True,
) -> Iterator[Any]:
    """Yield (context, page) for one board, signed in if we have a session.

    TWO LAYERS OF PERSISTENCE, and they do different jobs:

      * `user_data_dir` -- a real Chromium profile. Keeps the login on THIS
        machine and carries the things cookies alone do not (localStorage,
        service workers, the device fingerprint a board remembers you by).
      * `storage_state` -- a JSON cookie snapshot, loaded on top. Portable,
        inspectable, and the thing that survives a wiped profile directory.

    Loading the state on top of the profile is deliberate belt-and-braces:
    whichever of the two is fresher wins, and a board that logged the profile
    out can be recovered by re-importing the state rather than signing in
    again.
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
    profile_dir = SESSION_DIR / platform_slug(platform)
    profile_dir.mkdir(parents=True, exist_ok=True)
    state = storage_state_path(platform)

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

        if use_state and has_saved_session(platform):
            try:
                import json as _json

                cookies = _json.loads(state.read_text(encoding="utf-8"))
                if cookies.get("cookies"):
                    context.add_cookies(cookies["cookies"])
                    log.info("Restored %d saved cookie(s) for %s.",
                             len(cookies["cookies"]), platform)
            except Exception as exc:
                log.warning("Saved %s session could not be restored (%s); "
                            "continuing signed out.", platform, exc)

        page = context.pages[0] if context.pages else context.new_page()
        try:
            yield context, page
        finally:
            try:
                context.close()
            except Exception:
                pass


@contextmanager
def browser_page(
    platform: str = "default", headed: bool | None = None,
    use_state: bool = True,
) -> Iterator[Any]:
    """Yield just the page. The common case; `browser_context` when you need
    the context itself (to save the session after a login)."""
    with browser_context(platform, headed=headed, use_state=use_state) as (_ctx, page):
        yield page


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


# The control that reveals the real application form.
#
# On Tanqeeb -- and on most aggregators -- the job page you land on has NO
# application form at all. It has a description and a button. The form lives in
# a modal, a second route, or on the employer's own ATS, and none of it exists
# in the DOM until that button is clicked. Inspecting the landing page finds
# only the site's search widget, which `looks_like_application_form` then
# (correctly) refuses -- and the draft is unsubmittable for a reason that reads
# like a bug in the detector rather than a step never taken.
#
# Ordered most-specific first. A `[data-action=apply]` hook is unambiguous; a
# link whose text merely contains "apply" might be "apply filters".
APPLY_SELECTORS: tuple[str, ...] = (
    '[data-action="apply"]',
    ".apply-btn",
    ".btn-apply",
    "#apply-button",
    'a[href*="/apply"]',
    'button:has-text("قدّم الآن")',
    'button:has-text("قدم الآن")',
    'a:has-text("قدّم الآن")',
    'a:has-text("قدم الآن")',
    'button:has-text("تقدم للوظيفة")',
    'button:has-text("التقديم")',
    'button:has-text("Apply Now")',
    'a:has-text("Apply Now")',
    # Verified live on Tanqeeb, which renders both as
    # <a href="javascript:void(0)"> rather than as buttons or real links.
    'a:has-text("Apply on the Job Website")',
    'button:has-text("Easy Apply")',
    'button:has-text("Apply for this job")',
    'a:has-text("Apply for this job")',
    'button:has-text("Apply")',
    'a:has-text("Apply")',
)

#: Text that means "this is not the apply button", checked before clicking one
#: whose match was only textual. "Apply filters" and "Applied" are both common
#: on a board's own search chrome.
_NOT_APPLY = ("filter", "search", "applied", "sort", "apply coupon",
              "تصفية", "بحث")


def _apply_text_ok(element: Any, selector: str) -> bool:
    """Guard the loose text-matched selectors against the site's own chrome."""
    if selector.startswith(("[", ".", "#")):
        return True                       # explicit hooks need no second guess
    try:
        text = (element.inner_text() or "").strip().lower()
    except Exception:
        return True                       # unreadable: let the click decide
    return not any(bad in text for bad in _NOT_APPLY)


def _is_clickable(element: Any) -> bool:
    """Visible and enabled.

    Load-bearing on a real board. Tanqeeb renders "Apply Now" TWICE -- once in
    the article and once in a sticky bar that is hidden until you scroll -- and
    `page.click(selector)` picked the hidden one, waited out its actionability
    timeout, raised, and left the whole flow reporting "no apply control found"
    on a page that plainly had one.
    """
    try:
        if not element.is_visible():
            return False
    except Exception:
        return True                       # cannot tell: let the click decide
    try:
        return element.is_enabled()
    except Exception:
        return True


# Third-party sign-in buttons. These must NEVER be clicked.
#
# A "Continue with Google" button hands the flow to Google's OAuth consent
# screen, which refuses an automation-driven browser with
# "Error 400: redirect_uri_mismatch" -- an unrecoverable dead end mid-signup.
# The native email + password form is right there on the same page, and the
# vault already holds a strong per-board password for it.
SOCIAL_LOGIN_MARKERS: tuple[str, ...] = (
    "google", "facebook", "linkedin", "apple", "microsoft", "github",
    "twitter", "oauth", "sso",
)


def is_social_login(element: Any) -> bool:
    """True for a third-party sign-in control, which we must not touch."""
    blob = ""
    for getter in ("inner_text",):
        try:
            blob += " " + (getattr(element, getter)() or "")
        except Exception:
            pass
    for attribute in ("class", "href", "data-provider", "aria-label", "id"):
        try:
            blob += " " + (element.get_attribute(attribute) or "")
        except Exception:
            pass
    blob = blob.lower()
    return any(marker in blob for marker in SOCIAL_LOGIN_MARKERS)


def login_with_password(page: Any, email: str, password: str,
                        timeout_ms: int = 8000) -> tuple[bool, str]:
    """Sign in with the NATIVE email + password form. Never via OAuth.

    Returns (signed_in, what happened). Fails soft: a login that cannot be
    completed is reported, never raised, because the caller's next move is to
    hand the browser to the human either way.
    """
    fields = inspect_form(page)
    by_kind = {f.kind: f for f in fields}
    email_field = by_kind.get("email") or by_kind.get("username")
    password_field = by_kind.get("password")
    if email_field is None or password_field is None:
        return False, "no native email/password form on this page"

    if not fill_field(page, email_field, email):
        return False, "could not type the email address"
    if not fill_field(page, password_field, password):
        return False, "could not type the password"

    # Submit with the form's OWN control, filtered so a "Continue with Google"
    # button next to it can never be the one that gets pressed.
    for selector in SUBMIT_SELECTORS + ('button:has-text("Log in")',
                                        'button:has-text("Sign in")',
                                        'button:has-text("تسجيل الدخول")'):
        try:
            elements = page.query_selector_all(selector) or []
        except Exception:
            continue
        for element in elements:
            if is_social_login(element) or not _is_clickable(element):
                continue
            try:
                element.click(timeout=3000)
            except Exception:
                continue
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                pass
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
            remaining = {f.kind for f in inspect_form(page)}
            if "password" in remaining:
                return False, ("the password form is still showing -- wrong "
                               "credentials, or a verification step")
            return True, "signed in with email and password"
    return False, "no native submit control on the login form"


def never_automate_hosts() -> tuple[str, ...]:
    """Domains this project refuses to drive a browser against.

    Read from the same `auto_apply.never_automate` list the engine uses, so
    the rule is stated once. LinkedIn is on it because its anti-automation
    enforcement is the strictest of any source here and it is also the most
    productive one -- a flagged account costs far more than the applications
    it saved.
    """
    raw = (settings.raw.get("auto_apply", {}) or {}).get(
        "never_automate", ["linkedin"])
    return tuple(str(x).lower().strip() for x in raw if str(x).strip())


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_automatable_url(url: str) -> tuple[bool, str]:
    """May we drive a browser at this URL? (allowed, why not)."""
    host = _host_of(url)
    if not host:
        return True, ""
    for banned in never_automate_hosts():
        if host == banned or host.endswith("." + banned) or banned in host:
            return False, (
                f"this application is hosted on {host}, which is never "
                f"automated: applications there are manual-only, by design"
            )
    return True, ""


def load_saved_cookies(platform: str) -> list[dict[str, Any]]:
    """The cookies in a board's saved session. [] if there are none."""
    import json as _json

    path = storage_state_path(platform)
    if not path.exists():
        return []
    try:
        state = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [c for c in (state.get("cookies") or []) if isinstance(c, dict)]


def adopt_session_for(context: Any, url: str) -> str:
    """Inject the saved login of whichever board owns `url`. Returns its name.

    THE PROBLEM THIS SOLVES, measured live:

    A Tanqeeb posting's apply button opens Wuzzuf in a new tab -- but that tab
    belongs to the TANQEEB browser profile, which carries Tanqeeb's cookies and
    nothing else. So the bot arrived on Wuzzuf anonymous, was shown a
    registration form, and reported "not signed in" even though the Wuzzuf
    session was saved, valid, and sitting on disk unused.

    Aggregators hand off to each other constantly, so the session has to follow
    the hop. Cookies are added to the CURRENT context rather than reopening the
    page in another one, because the popup already exists and re-navigating it
    is cheaper and keeps the referer chain intact.
    """
    if context is None or not url:
        return ""
    try:
        from auto_apply.profile_builder import platform_for_url
    except Exception:
        return ""

    board = platform_for_url(url)
    if board is None:
        return ""
    cookies = load_saved_cookies(board.slug)
    if not cookies:
        return ""
    try:
        context.add_cookies(cookies)
    except Exception as exc:
        log.debug("Could not adopt the %s session: %s", board.name, exc)
        return ""
    log.info("Adopted the saved %s session (%d cookies) for %s",
             board.name, len(cookies), url[:60])
    return board.name


def open_application_form(
    page: Any, timeout_ms: int = 5000, max_hops: int = 3
) -> tuple[Any, bool, str]:
    """Click through to the real application view, following the whole chain.

    Aggregators hand off to each other. Measured live: a Tanqeeb posting's
    apply button opens Wuzzuf in a new tab, and Wuzzuf then has its own "Apply
    For Job" button before any form exists. One hop is not enough, so this
    keeps going until it reaches something that looks like an application form,
    runs out of controls, or is refused.
    """
    notes: list[str] = []
    current = page
    opened_any = False

    for _hop in range(max(1, max_hops)):
        current, opened, note = _open_application_once(current, timeout_ms)
        if note:
            notes.append(note)
        if not opened:
            break
        opened_any = True
        if looks_like_application_form(inspect_form(current))[0]:
            break

    return current, opened_any, " -> ".join(notes)


def _open_application_once(
    page: Any, timeout_ms: int = 5000
) -> tuple[Any, bool, str]:
    """One hop: click the apply control on THIS page and follow where it goes.

    Returns `(page_to_use, opened, note)`. The page comes back because the
    click routinely opens a NEW TAB rather than changing the current one --
    verified live on Tanqeeb, where "Apply on the Job Website" pops the
    employer's own site and leaves the original page untouched. A caller that
    kept inspecting the original page would see the job description forever and
    conclude, wrongly, that there is no application form anywhere.

    `opened` is False when there was nothing to click, which is the normal case
    on a page that already IS the form -- so callers can treat "no apply
    button" as "already there" rather than as a failure.

    A popup that lands on a never-automate domain is CLOSED and refused. That
    is not a failure either: it is the LinkedIn rule holding at the one moment
    it matters, and the note says so in words the user can act on.
    """
    already = inspect_form(page)
    if looks_like_application_form(already)[0]:
        return page, False, "the page already shows an application form"

    before_url = str(getattr(page, "url", "") or "")
    context = getattr(page, "context", None)

    def open_pages() -> list[Any]:
        try:
            return list(context.pages) if context is not None else []
        except Exception:
            return []

    pages_before = open_pages()

    for selector in APPLY_SELECTORS:
        # Resolve to ELEMENTS, then pick one that can actually be clicked.
        #
        # Two reasons this is not `page.click(selector)`:
        #   * cost -- clicking speculatively down the whole list pays the
        #     actionability timeout on every MISS, and most of this list misses
        #     on any given page. Eighteen selectors at 2.5s is forty-five
        #     seconds of dead waiting; measured live at roughly six minutes per
        #     job, nearly all of it here.
        #   * correctness -- a board that renders the same control twice (one
        #     in the article, one in a sticky bar that is hidden until you
        #     scroll) hands `page.click` the hidden copy first, which times out
        #     and takes the whole flow down with it.
        try:
            elements = page.query_selector_all(selector) or []
        except Exception:
            continue

        clicked_element = False
        for element in elements:
            if not _is_clickable(element) or not _apply_text_ok(element, selector):
                continue
            try:
                # Scroll first. A board can render the control in a bar that
                # sits OUTSIDE the viewport -- measured on Wuzzuf, where the
                # first "Apply For Job" button reports a bounding box at
                # y=-222 and Playwright refuses to click it however long it
                # waits. Scrolling rescues the reachable ones; the loop moves
                # on for the rest.
                element.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            try:
                element.click(timeout=2500)
                clicked_element = True
                break
            except Exception:
                # Last resort: dispatch the click in the page. It skips
                # Playwright's actionability checks, which is exactly what an
                # off-canvas-but-real button needs -- and the element already
                # passed the visible/enabled/text gates above.
                try:
                    element.evaluate("node => node.click()")
                    clicked_element = True
                    break
                except Exception:
                    continue
        if not clicked_element:
            continue

        # Let whichever of the three things happen, happen.
        for settle in (
            lambda: page.wait_for_load_state("networkidle", timeout=timeout_ms),
            lambda: page.wait_for_load_state("domcontentloaded",
                                             timeout=timeout_ms),
        ):
            try:
                settle()
                break
            except Exception:
                continue
        try:
            page.wait_for_timeout(800)    # modal animation, lazy form render
        except Exception:
            pass

        # Did the click open a NEW TAB? On an aggregator this is the common
        # case, not the exception.
        #
        # POLLED, not checked once. A tab takes a moment to register with the
        # context, and a single look right after the click misses it -- which
        # showed up live as "the form opened in place" on a page that had in
        # fact just popped the employer's site into a second tab.
        popup = None
        for _attempt in range(16):
            popup = next((p for p in open_pages() if p not in pages_before),
                         None)
            if popup is not None:
                break
            try:
                page.wait_for_timeout(250)
            except Exception:
                break
        if popup is not None:
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except Exception:
                pass
            try:
                popup.wait_for_timeout(800)
            except Exception:
                pass
            popup_url = str(getattr(popup, "url", "") or "")

            allowed, refusal = is_automatable_url(popup_url)
            if not allowed:
                # Close it. Leaving a LinkedIn tab open in a persistent,
                # logged-in browser profile is exactly the footprint the rule
                # exists to avoid.
                try:
                    popup.close()
                except Exception:
                    pass
                log.warning("Refusing the external application: %s", refusal)
                return page, False, refusal

            # The tab belongs to THIS board's browser profile, so a hop to a
            # different board arrives signed out unless its cookies come too.
            adopted = adopt_session_for(context, popup_url)
            if adopted:
                try:
                    popup.reload(wait_until="domcontentloaded",
                                 timeout=timeout_ms)
                    popup.wait_for_timeout(1200)
                except Exception:
                    pass

            log.info("The application opened in a new tab: %s", popup_url[:90])
            detail = (f"clicked {selector}; the application opened in a new "
                      f"tab at {popup_url[:80]}")
            if adopted:
                detail += f" (signed in as your {adopted} account)"
            return popup, True, detail

        after_url = str(getattr(page, "url", "") or "")
        moved = after_url and after_url != before_url
        if moved:
            allowed, refusal = is_automatable_url(after_url)
            if not allowed:
                log.warning("Refusing the external application: %s", refusal)
                return page, False, refusal
            # Same handoff for an in-place redirect to another board.
            if adopt_session_for(context, after_url):
                try:
                    page.reload(wait_until="domcontentloaded",
                                timeout=timeout_ms)
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
        detail = (f"clicked {selector} and the page moved to {after_url[:80]}"
                  if moved else f"clicked {selector}; the form opened in place")
        log.info("Opened the application view: %s", detail)
        return page, True, detail

    return page, False, "no apply control found on this page"


def inspect_form(page: Any, root_selector: str = "form") -> list[FormField]:
    """Describe every fillable field on the page.

    Returns semantic descriptions, not selectors alone, so the caller can decide
    what it knows how to answer and what needs the AI.
    """
    fields: list[FormField] = []
    seen: set[str] = set()

    # Retry once through a navigation.
    #
    # Clicking an apply control often starts a navigation, and querying the
    # page mid-flight raises "Execution context was destroyed". That surfaced
    # as a draft that could not be re-inspected AT ALL -- a transient timing
    # accident presented to the user as a permanent failure. Settling and
    # asking again costs a moment and removes the whole class.
    containers = None
    for attempt in range(2):
        try:
            containers = page.query_selector_all(root_selector) or [page]
            break
        except Exception as exc:
            if attempt:
                log.warning("Could not read the page to inspect it: %s", exc)
                return []
            for settle in ("domcontentloaded", "networkidle"):
                try:
                    page.wait_for_load_state(settle, timeout=8000)
                except Exception:
                    continue
    if containers is None:
        return []
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

    # A PASSWORD FIELD MEANS THIS IS A LOGIN OR SIGNUP, NEVER AN APPLICATION.
    #
    # Checked before anything else, because this form is otherwise a perfect
    # impostor: Wuzzuf's registration page asks for first name, last name and
    # email, which is exactly the "cluster of personal details" that proves an
    # application form below. Filling it and pressing submit would create an
    # account -- or fail against an existing one -- and then bank the result as
    # a delivered application. That is the same looks-like-success failure as
    # submitting the site's search widget, and it is reached by the same route:
    # the board showed a sign-in wall because we are not actually logged in.
    if "password" in kinds:
        return False, (
            "this is a sign-in or registration form, not an application form "
            "-- the board is asking you to log in. Run: "
            "python main.py --register <board>"
        )

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


# Markers that mean a human challenge stands between us and the submit button.
# Matched against page markup rather than rendered text: reCAPTCHA and Turnstile
# render inside a cross-origin iframe whose text we cannot read, but the iframe
# itself is always in the DOM.
_CAPTCHA_MARKERS: tuple[tuple[str, str], ...] = (
    ("g-recaptcha", "Google reCAPTCHA"),
    ("recaptcha/api", "Google reCAPTCHA"),
    ("hcaptcha", "hCaptcha"),
    ("cf-turnstile", "Cloudflare Turnstile"),
    ("challenges.cloudflare.com", "Cloudflare challenge"),
    ("funcaptcha", "FunCaptcha / Arkose"),
    ("arkoselabs", "FunCaptcha / Arkose"),
    ("geetest", "GeeTest"),
    ("data-sitekey", "a CAPTCHA widget"),
    ("captcha", "a CAPTCHA"),
    ("أنا لست روبوت", "a CAPTCHA"),
    ("i'm not a robot", "a CAPTCHA"),
)


def detect_captcha(page: Any) -> tuple[bool, str]:
    """Is a human-verification challenge on this page?

    This runs BEFORE the submit click, and a positive result stops the flow.
    The reason is the failure mode it prevents, which is the same one
    `looks_like_application_form` exists for: clicking "submit" underneath an
    unsolved CAPTCHA does not raise. The page re-renders with an error, the
    screenshot captures that error page, and the application is banked as
    `submitted`. The user then waits for a reply to something that was never
    delivered -- which is strictly worse than a recorded failure, because it
    looks like success.

    Never raises: an inspection that cannot run must not be able to block a
    submission that would otherwise have worked.
    """
    try:
        markup = page.content() or ""
    except Exception as exc:
        log.debug("Could not read the page for CAPTCHA detection: %s", exc)
        return False, ""

    haystack = markup.lower()
    for needle, name in _CAPTCHA_MARKERS:
        if needle.lower() in haystack:
            log.warning("CAPTCHA detected on the application page: %s.", name)
            return True, name
    return False, ""


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


# Selectors tried in order to find the form's own submit control. Bilingual,
# because half these boards render in Arabic.
SUBMIT_SELECTORS: tuple[str, ...] = (
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Apply")',
    'button:has-text("Submit")',
    'button:has-text("Register")',
    'button:has-text("Sign up")',
    'button:has-text("Create account")',
    'button:has-text("تقديم")',
    'button:has-text("إرسال")',
    'button:has-text("تسجيل")',
    'button:has-text("إنشاء حساب")',
)


def click_submit(page: Any, timeout_ms: int = 5000) -> str:
    """Press the form's own submit control. Returns the selector, or "".

    Shared between registration and application on purpose: they were two
    separate lists that had already drifted -- the application flow knew the
    Arabic "تقديم" and the registration flow knew none of it, so an
    Arabic-rendered signup page had no submit button as far as the bot was
    concerned.
    """
    for selector in SUBMIT_SELECTORS:
        try:
            page.click(selector, timeout=timeout_ms)
            log.info("Submitted via %s", selector)
            return selector
        except Exception:
            continue
    return ""


# ---------------------------------------------------------------------------
# Applicant tracking systems
# ---------------------------------------------------------------------------
# Most real application forms are not built by the job board -- they are an
# embedded ATS, and each one has its own shape. Knowing WHICH one is on the
# page is what turns "a form" into "a form I know is multi-step and where the
# CV upload is behind a button rather than an <input type=file>".
ATS_MARKERS: tuple[tuple[str, str], ...] = (
    ("myworkdayjobs.com", "workday"),
    ("workday", "workday"),
    ("greenhouse.io", "greenhouse"),
    ("grnhse", "greenhouse"),
    ("lever.co", "lever"),
    ("smartrecruiters", "smartrecruiters"),
    ("taleo.net", "taleo"),
    ("taleo", "taleo"),
    ("icims.com", "icims"),
    ("successfactors", "successfactors"),
    ("ashbyhq.com", "ashby"),
    ("bamboohr.com", "bamboohr"),
    ("recruitee.com", "recruitee"),
    ("workable.com", "workable"),
    ("jobvite", "jobvite"),
)

#: ATSes whose application is a WIZARD, not one page. Submitting these means
#: walking "Next" until the final button appears.
MULTI_STEP_ATS = frozenset({"workday", "taleo", "icims", "successfactors",
                            "smartrecruiters"})


def detect_ats(page: Any) -> str:
    """Name the applicant tracking system behind this page, or "".

    Checks the URL first -- it is the strongest signal and cannot be faked by
    page copy -- then the markup, which catches an ATS embedded in an iframe on
    the employer's own domain.
    """
    try:
        url = str(getattr(page, "url", "") or "").lower()
    except Exception:
        url = ""
    for needle, name in ATS_MARKERS:
        if needle in url:
            return name
    try:
        markup = (page.content() or "").lower()
    except Exception:
        return ""
    for needle, name in ATS_MARKERS:
        if needle in markup:
            return name
    return ""


# Controls that move a wizard FORWARD without finishing it. Kept strictly
# separate from SUBMIT_SELECTORS: clicking "Submit" when you meant "Next"
# files a half-empty application, which is unrecoverable.
NEXT_SELECTORS: tuple[str, ...] = (
    'button:has-text("Save and Continue")',
    'button:has-text("Continue")',
    'button:has-text("Next")',
    'button:has-text("Save & Continue")',
    'button[data-automation-id="bottom-navigation-next-button"]',
    'a:has-text("Continue")',
    'button:has-text("التالي")',
    'button:has-text("متابعة")',
)


def has_submit(page: Any) -> bool:
    """Is a real submit control present on THIS page of the form?

    The wizard walker uses this to know when to stop advancing. It only looks
    -- it never clicks -- because the difference between "Next" and "Submit" is
    the difference between filling page two and filing a half-empty
    application in someone's name.
    """
    for selector in SUBMIT_SELECTORS:
        try:
            if page.query_selector(selector) is not None:
                return True
        except Exception:
            continue
    return False


def click_next(page: Any, timeout_ms: int = 4000) -> str:
    """Advance one page of a multi-step form. Returns the selector, or ""."""
    for selector in NEXT_SELECTORS:
        try:
            page.click(selector, timeout=timeout_ms)
            log.info("Advanced the form via %s", selector)
            return selector
        except Exception:
            continue
    return ""


def attach_cv(page: Any, field_: "FormField | None", cv_path: str) -> bool:
    """Put the CV into the page, whichever way this form accepts one.

    Three routes, tried in order, because "upload your CV" is rendered three
    different ways in the wild:

      1. A real `<input type=file>` -- `set_input_files` on its selector.
      2. A hidden input behind a styled button -- `set_input_files` on the
         first file input on the page, ignoring the visible control entirely.
      3. A button that opens the OS file chooser with no input to target --
         caught with `expect_file_chooser`.

    Route 2 is the one that matters most in practice: Greenhouse, Lever and
    almost every modern ATS hide the real input and style a <div> over it, so
    matching on the visible label alone finds a button that cannot be filled.
    """
    if not cv_path:
        return False

    if field_ is not None:
        try:
            page.set_input_files(field_.selector, cv_path, timeout=15000)
            return True
        except Exception as exc:
            log.debug("CV upload via the detected field failed: %s", exc)

    try:
        page.set_input_files('input[type="file"]', cv_path, timeout=8000)
        log.info("Attached the CV to the page's hidden file input.")
        return True
    except Exception as exc:
        log.debug("CV upload via the first file input failed: %s", exc)

    for selector in ('button:has-text("Upload")', 'button:has-text("Attach")',
                     'label:has-text("Resume")', 'button:has-text("Resume")',
                     'button:has-text("CV")'):
        try:
            with page.expect_file_chooser(timeout=5000) as chooser_info:
                page.click(selector, timeout=4000)
            chooser_info.value.set_files(cv_path)
            log.info("Attached the CV through the file chooser (%s).", selector)
            return True
        except Exception:
            continue

    log.warning("Could not attach the CV by any route.")
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
