"""
Assisted account creation and profile completion.

WHY "ASSISTED" AND NOT FULLY AUTOMATIC
--------------------------------------
Registering accounts by bot is the single fastest way to lose them. Signup flows
on these boards sit behind CAPTCHA, email and often phone verification, and are
watched by the same anti-automation systems that make LinkedIn off-limits here.
Driving them headlessly means defeating those checks, and the realistic outcome
is a banned account on a board the job hunt depends on -- strictly worse than
having no account at all.

So the browser does the tedious 95%: it opens the right page, derives a strong
per-platform password, fills every field it can identify from the CV, and
uploads the PDF. YOU solve the CAPTCHA and press the final button. Then the
credentials are sealed into the vault and pushed to Telegram.

The result is the same finished profile, in about twenty seconds of attention
per board, with none of the ban risk.

PASSWORDS
---------
Each platform gets a DIFFERENT password derived from APPLY_BASE_PASSWORD via
HMAC-SHA256. One board's breach therefore cannot be replayed against the
others, and the passwords are still reproducible from the seed if the vault is
ever lost.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from typing import Any

from config import settings
from vault import SecureStore

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Platform:
    name: str
    url: str
    login_url: str = ""
    #: Domains whose job pages belong to this board. This is what maps a
    #: scraped JOB URL back to the saved login that can actually open it.
    hosts: tuple[str, ...] = ()
    #: Set for boards that make a bot-driven signup a bad trade -- phone
    #: verification, a standing CAPTCHA, or an anti-automation reputation.
    #: Those are provisioned into the vault but never auto-submitted.
    manual_signup: bool = False
    #: True when the application form only exists for a signed-in candidate.
    needs_login: bool = True

    @property
    def slug(self) -> str:
        """Filesystem-safe key: the session-state filename and profile dir."""
        return re.sub(r"[^a-z0-9]+", "_", self.name.lower()).strip("_")

    def owns(self, url: str) -> bool:
        """Does this board serve that job URL?

        Matched on the registrable domain rather than a substring, so
        `notbayt.com.evil.test` cannot borrow Bayt's saved cookies.
        """
        host = _host_of(url)
        return any(host == h or host.endswith("." + h) for h in self.hosts)


def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlsplit

        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def configured_platforms() -> list[Platform]:
    raw = (settings.raw.get("auto_apply", {}) or {}).get("platforms", []) or []
    return [
        Platform(
            name=str(p.get("name", "")), url=str(p.get("url", "")),
            login_url=str(p.get("login_url", "")),
            hosts=tuple(
                str(h).lower().lstrip(".") for h in (p.get("hosts") or [])
            ),
            manual_signup=bool(p.get("manual_signup", False)),
            needs_login=bool(p.get("needs_login", True)),
        )
        for p in raw if p.get("name") and p.get("url")
    ]


def find_platform(name: str) -> Platform | None:
    wanted = (name or "").strip().lower()
    return next((p for p in configured_platforms()
                 if p.name.lower() == wanted), None)


def platform_for_url(url: str) -> Platform | None:
    """Which board serves this job URL? None when it is nobody's.

    The apply flow needs this to pick the right saved login. A posting that
    belongs to no configured board -- an employer's own careers page reached
    from RemoteOK, say -- correctly returns None and is opened signed-out,
    which is the right behaviour there.
    """
    if not url:
        return None
    return next((p for p in configured_platforms() if p.owns(url)), None)


def platform_for_source(source_platform: str, url: str = "") -> Platform | None:
    """Resolve a board from the scraper's source tag, falling back to the URL.

    Sources are tagged `tanqeeb:egypt`, `talent:ae`, `api:remoteok` -- the part
    before the colon is the board. The URL is the more reliable signal, so it
    wins; the tag is what rescues a posting whose link points at a redirect.
    """
    board = platform_for_url(url)
    if board is not None:
        return board
    head = (source_platform or "").split(":")[0].strip().lower()
    if not head:
        return None
    for platform in configured_platforms():
        if platform.slug == head or platform.name.lower() == head:
            return platform
        if any(h.split(".")[0] == head for h in platform.hosts):
            return platform
    return None


@dataclass(slots=True)
class ProfilePayload:
    """Everything a board's signup form can ask for, resolved once.

    Built here rather than read field-by-field inside the browser loop so the
    SAME profile can be provisioned into the vault without opening a browser at
    all -- which is what makes `--provision` a safe, offline operation.
    """

    platform: str
    url: str
    full_name: str
    email: str
    username: str
    password: str
    phone: str
    location: str
    headline: str
    bio: str
    cv_path: str

    def form_values(self) -> dict[str, str]:
        """Semantic field name -> value, for `auto_apply.browser` to fill."""
        from auto_apply.candidate import load_candidate

        values = load_candidate().form_values()
        values.update({
            "email": self.email,
            "username": self.username,
            "password": self.password,
            "phone": self.phone,
            "location": self.location,
            "headline": self.headline,
            "bio": self.bio,
            "full_name": self.full_name,
        })
        return {k: v for k, v in values.items() if v}

    def summary_line(self) -> str:
        return (f"{self.platform}: {self.email} / {self.username} "
                f"({'CV ready' if self.cv_path else 'NO CV ON DISK'})")


def build_profile(platform: Platform) -> ProfilePayload:
    """Resolve the full signup payload for one board. No network, no browser."""
    from auto_apply.candidate import load_candidate

    candidate = load_candidate()
    identity = (settings.raw.get("auto_apply", {}) or {}).get("identity", {}) or {}
    email = str(identity.get("email") or settings.job_email or candidate.email)
    return ProfilePayload(
        platform=platform.name,
        url=platform.url,
        full_name=candidate.full_name,
        email=email,
        username=str(identity.get("username") or candidate.username),
        password=derive_password(platform.name),
        phone=str(identity.get("phone") or candidate.phone),
        location=str(identity.get("location") or candidate.location),
        headline=str(identity.get("headline") or candidate.headline),
        bio=str(identity.get("bio") or candidate.summary),
        cv_path=next((str(p) for p in settings.cv_paths if p.exists()), ""),
    )


def provision(platform: Platform, store: SecureStore,
              notifier: Any = None) -> ProfilePayload:
    """Derive and vault this board's credentials. Opens nothing, sends nothing.

    Separated from `prefill_registration` deliberately. Deriving a password and
    banking it is offline, reversible and safe to run for every board at once;
    creating an account is none of those. Running this first means the vault is
    already correct before any browser opens, so a signup interrupted half way
    still leaves a recoverable credential rather than a password that existed
    only in a closed window.
    """
    profile = build_profile(platform)
    existing = store.get_credentials(platform.name) or {}
    store.save_credentials(
        platform_name=platform.name, platform_url=platform.url,
        email=profile.email,
        # A handle the BOARD issued outranks the one we derived: boards hand you
        # your real username only after signup, and overwriting it with a guess
        # leaves a vault entry that cannot log anyone in.
        username=existing.get("username") or profile.username,
        # A password already in the vault WINS over the derived one.
        #
        # The derived password is a sensible default for a board with no
        # account yet. It is not a reason to overwrite the real password of an
        # account that already exists -- and re-deriving over one would leave a
        # vault entry that cannot log in, silently, until the next apply run
        # failed for a reason that looked like anything but this.
        password=existing.get("password") or profile.password,
        # Likewise the status: a re-sync must not un-finish an account that has
        # already been created.
        profile_status=existing.get("profile_status") or "provisioned",
        notes=f"headline: {profile.headline[:60]}",
    )
    log.info("Provisioned %s", profile.summary_line())
    return profile


def provision_all(store: SecureStore) -> list[ProfilePayload]:
    """Vault credentials for every configured board."""
    return [provision(p, store) for p in configured_platforms()]


def derive_password(platform_name: str, base: str = "", length: int = 18) -> str:
    """A distinct, strong password per platform, reproducible from the seed.

    HMAC keyed on the base secret, so the per-site passwords cannot be worked
    backwards into the seed, and every site gets a different one. The suffix
    guarantees the character-class rules boards tend to enforce.
    """
    seed = base or settings.apply_base_password
    if not seed:
        raise RuntimeError(
            "APPLY_BASE_PASSWORD is not set; cannot derive platform passwords."
        )
    digest = hmac.new(
        seed.encode("utf-8"),
        f"ai-job-hunter::{platform_name.strip().lower()}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    body = base64.urlsafe_b64encode(digest).decode("ascii")
    body = body.replace("-", "x").replace("_", "y").replace("=", "")[: max(8, length - 4)]
    # Guarantee upper, lower, digit and symbol regardless of the digest.
    return f"{body}A9q@"


def format_account_message(
    platform: Platform, profile: "ProfilePayload", status: str,
    filled: list[str] | None = None,
) -> str:
    lines = [
        "🔐 *NEW PLATFORM ACCOUNT CREATED*",
        f"🌐 Platform: {platform.name} ({platform.url})",
        f"📧 Email: {profile.email}",
        f"👤 Username: {profile.username}",
        f"🔑 Password: {profile.password}",
        f"🏷️ Headline: {profile.headline}",
        f"✅ Profile: {status}",
    ]
    if filled:
        lines.append(f"📋 Pre-filled: {', '.join(filled[:12])}")
    lines += [
        "",
        "_Stored encrypted in the local vault. This message is in your own "
        "Saved Messages -- delete it once you have the password in a manager._",
    ]
    return "\n".join(lines)


def prefill_registration(
    platform: Platform, store: SecureStore, notifier: Any = None,
    headed: bool = True, auto_submit: bool | None = None,
) -> dict[str, Any]:
    """Open the signup page, fill everything derivable, and finish it.

    HOW FAR THIS GOES, AND WHY
    --------------------------
    The credentials are vaulted BEFORE the browser opens, so a signup that dies
    half way still leaves a recoverable password rather than one that existed
    only in a closed window.

    Then every field the inspector can name is filled -- contact details, the
    professional headline, the bio, the CV upload -- and the form is submitted
    automatically UNLESS a human-verification challenge is detected, in which
    case it stops and waits for you. That is the one thing a bot must not push
    through: defeating a CAPTCHA is what turns "an account" into "a banned
    account on a board the job hunt depends on".

    Boards marked `manual_signup` in config.yml are never auto-submitted at all
    -- they are the ones with phone verification, where a half-made account is
    worse than none.
    """
    from auto_apply.browser import (
        browser_context, capture_evidence, click_submit, detect_captcha,
        fill_field, inspect_form, save_storage_state,
    )

    if auto_submit is None:
        auto_submit = bool(
            (settings.raw.get("auto_apply", {}) or {})
            .get("auto_submit_registration", True)
        )
    if platform.manual_signup:
        auto_submit = False

    # Vault first. See the docstring.
    profile = provision(platform, store)
    values = profile.form_values()

    report: dict[str, Any] = {
        "platform": platform.name, "email": profile.email,
        "username": profile.username, "filled": [], "unfilled": [],
        "screenshot": "", "captcha": "", "submitted": False,
        "status": "pending", "session_saved": "",
    }

    with browser_context(platform.slug, headed=headed) as (context, page):
        page.goto(platform.url, wait_until="domcontentloaded")
        fields = inspect_form(page)

        for f in fields:
            if f.kind == "resume" and profile.cv_path:
                if fill_field(page, f, profile.cv_path):
                    report["filled"].append("resume (PDF uploaded)")
                continue
            value = values.get(f.kind, "")
            if value and fill_field(page, f, value):
                report["filled"].append(f.kind)
            elif f.kind != "unknown":
                report["unfilled"].append(f.kind)

        blocked, challenge = detect_captcha(page)
        report["captcha"] = challenge

        if blocked or not auto_submit:
            reason = (f"{challenge} must be solved by a human"
                      if blocked else
                      "this board is marked manual-signup"
                      if platform.manual_signup else
                      "auto_submit_registration is off")
            log.warning("%s: pre-filled %d field(s) -- NOT submitting: %s.",
                        platform.name, len(report["filled"]), reason)
            report["status"] = "awaiting_user_submit"
            if headed:
                try:
                    page.wait_for_timeout(1000)
                    input(
                        f"\n  >> {platform.name} is open and pre-filled.\n"
                        f"     {reason}.\n"
                        f"     Finish and submit in the browser, then press "
                        f"Enter here... "
                    )
                    report["status"] = "complete"
                except (EOFError, KeyboardInterrupt):
                    log.warning("No confirmation given; recorded as pending.")
        else:
            selector = click_submit(page)
            if selector:
                report["submitted"] = True
                report["status"] = "complete"
                try:
                    page.wait_for_load_state("networkidle", timeout=30000)
                except Exception:
                    pass
            else:
                log.warning("%s: no submit control found; leaving it to you.",
                            platform.name)
                report["status"] = "awaiting_user_submit"

        report["screenshot"] = capture_evidence(page, f"register_{platform.name}")

        # BANK THE LOGIN. This is the payoff for the whole flow: with these
        # cookies, `--apply` opens the board's job pages as a signed-in
        # candidate and sees the real application form. Without them it gets
        # the public landing page, whose only form is the site search -- which
        # is exactly why every draft so far has been refused at the submit gate.
        #
        # Saved even when the form was NOT submitted: you may have signed into
        # an existing account rather than creating one, and that session is
        # just as useful.
        report["session_saved"] = save_storage_state(context, platform.slug)

    store.set_profile_status(platform.name, report["status"])
    status_text = {
        "complete": "Submitted — profile ready",
        "awaiting_user_submit": "Pre-filled — awaiting your submit",
    }.get(report["status"], report["status"])
    if notifier:
        from auto_apply.review import dispatch_text

        dispatch_text(
            notifier,
            format_account_message(platform, profile, status_text,
                                   report["filled"]),
            format_account_message(platform, profile, status_text),
        )
    return report


def store_existing_account(
    platform_name: str, email: str, password: str, store: SecureStore,
    platform_url: str = "", notifier: Any = None,
) -> int:
    """Vault an account you created yourself, by hand.

    The realistic path for boards with phone verification: sign up normally,
    then record it here so the apply engine can log in later.
    """
    row_id = store.save_credentials(
        platform_name=platform_name, platform_url=platform_url, email=email,
        password=password, profile_status="complete", notes="added manually",
    )
    if notifier:
        notifier.send_via_telegram(
            "🔐 *ACCOUNT ADDED TO VAULT*\n"
            f"🌐 Platform: {platform_name}\n"
            f"📧 Email: {email}\n"
            "✅ Stored encrypted locally."
        )
    return row_id
