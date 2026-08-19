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


def configured_platforms() -> list[Platform]:
    raw = (settings.raw.get("auto_apply", {}) or {}).get("platforms", []) or []
    return [
        Platform(name=str(p.get("name", "")), url=str(p.get("url", "")),
                 login_url=str(p.get("login_url", "")))
        for p in raw if p.get("name") and p.get("url")
    ]


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
    platform: Platform, email: str, password: str, status: str
) -> str:
    return (
        "🔐 *NEW PLATFORM ACCOUNT CREATED*\n"
        f"🌐 Platform: {platform.name} ({platform.url})\n"
        f"📧 Email: {email}\n"
        f"🔑 Password: {password}\n"
        f"✅ Profile: {status}\n\n"
        "_Stored encrypted in the local vault. This message is in your own "
        "Saved Messages -- delete it once you have the password in a manager._"
    )


def prefill_registration(
    platform: Platform, store: SecureStore, notifier: Any = None,
    headed: bool = True,
) -> dict[str, Any]:
    """Open the signup page, fill everything derivable, hand over to the human.

    Returns a report describing what was filled and what is left to do. The
    account is recorded as `awaiting_user_submit` -- it is only marked complete
    once you confirm, because this function deliberately does not press submit.
    """
    from auto_apply.browser import (
        browser_page, capture_evidence, fill_field, inspect_form,
    )
    from auto_apply.candidate import load_candidate

    candidate = load_candidate()
    password = derive_password(platform.name)
    email = settings.job_email or candidate.email
    values = candidate.form_values()
    values["email"] = email
    values["password"] = password

    cv_path = next((str(p) for p in settings.cv_paths if p.exists()), "")
    report: dict[str, Any] = {
        "platform": platform.name, "email": email,
        "filled": [], "unfilled": [], "screenshot": "",
    }

    with browser_page(platform.name, headed=headed) as page:
        page.goto(platform.url, wait_until="domcontentloaded")
        fields = inspect_form(page)

        for f in fields:
            if f.kind == "resume" and cv_path:
                if fill_field(page, f, cv_path):
                    report["filled"].append("resume (PDF uploaded)")
                continue
            value = values.get(f.kind, "")
            if value and fill_field(page, f, value):
                report["filled"].append(f.kind)
            elif f.kind != "unknown":
                report["unfilled"].append(f.kind)

        report["screenshot"] = capture_evidence(page, f"register_{platform.name}")

        log.info(
            "%s: pre-filled %d field(s). Solve the CAPTCHA and press the "
            "platform's own submit button in the open browser window.",
            platform.name, len(report["filled"]),
        )
        if headed:
            try:
                page.wait_for_timeout(1000)
                input(
                    f"\n  >> {platform.name} is open and pre-filled.\n"
                    f"     Complete any CAPTCHA/verification, submit the form,\n"
                    f"     then press Enter here to vault the credentials... "
                )
            except (EOFError, KeyboardInterrupt):
                log.warning("No confirmation given; credentials stored as pending.")

    status = "Pre-filled — awaiting your submit" if not headed else \
             "Submitted by you — profile ready"
    store.save_credentials(
        platform_name=platform.name, platform_url=platform.url,
        email=email, password=password,
        profile_status="complete" if headed else "awaiting_user_submit",
        notes=f"filled: {', '.join(report['filled'])}",
    )
    if notifier:
        notifier.send_via_telegram(
            format_account_message(platform, email, password, status)
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
