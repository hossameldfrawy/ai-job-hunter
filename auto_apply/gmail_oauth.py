"""
Gmail API access via OAuth2.

Google has stopped issuing App Passwords on newer accounts -- the setting simply
does not appear -- so IMAP with a password is no longer a route in at all. The
supported path is OAuth2 against the Gmail API, which is also strictly better:
the token is scoped to Gmail alone, it can be revoked from the Google account
page without changing any password, and reading a message never marks it read
unless we explicitly say so.

WHAT YOU NEED ONCE
------------------
A Google Cloud OAuth client, because OAuth identifies the *application*, not
just the user. There is no way around this and no credential can be shipped in
the repository for it:

  1. https://console.cloud.google.com/  ->  create (or pick) a project
  2. APIs & Services -> Library -> enable "Gmail API"
  3. APIs & Services -> OAuth consent screen -> External -> add
     hossam.eldefrawy.dev@gmail.com as a Test user
  4. Credentials -> Create credentials -> OAuth client ID -> Desktop app
  5. Download the JSON to  secrets/gmail_client_secret.json

Then run `python auth_gmail.py` once. A browser opens, you approve, and the
resulting token lands in secrets/gmail_token.json. From then on it refreshes
itself; you will not be asked again unless the token is revoked.

Both files are git-ignored. The token is equivalent to mailbox access, so it is
treated exactly like the vault: local only, never synced, never published.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from config import ROOT

log = logging.getLogger(__name__)

SECRETS_DIR = ROOT / "secrets"
CLIENT_SECRET_PATH = SECRETS_DIR / "gmail_client_secret.json"
TOKEN_PATH = SECRETS_DIR / "gmail_token.json"

# `modify` rather than `readonly` because the monitor marks job mail as read
# once it has been classified. That is the narrowest scope that still allows it;
# it grants no access to Drive, Contacts or anything outside Gmail.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

SETUP_HELP = f"""\
Gmail API access is not set up yet.

App Passwords are unavailable on newer Google accounts, so OAuth2 is the only
supported way in. One-time setup:

  1. https://console.cloud.google.com/ -> create or select a project
  2. APIs & Services -> Library -> enable "Gmail API"
  3. APIs & Services -> OAuth consent screen -> External
       -> add your job-hunt address under "Test users"
  4. Credentials -> Create credentials -> OAuth client ID -> Desktop app
  5. Download the JSON as:
       {CLIENT_SECRET_PATH}

Then run:  python auth_gmail.py
"""


class GmailAuthError(RuntimeError):
    pass


def libraries_available() -> bool:
    try:
        import google.auth  # noqa: F401
        import googleapiclient  # noqa: F401
        import google_auth_oauthlib  # noqa: F401

        return True
    except ImportError:
        return False


def is_configured() -> bool:
    """True when a token exists, i.e. the Gmail API backend can be used."""
    return TOKEN_PATH.exists() and libraries_available()


def _protect_secrets_dir() -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    gitignore = SECRETS_DIR / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")


def load_credentials(interactive: bool = False) -> Any:
    """Return usable Credentials, refreshing or re-authorising as needed.

    `interactive=False` never opens a browser: it either returns working
    credentials or raises. That keeps a scheduled run from silently blocking
    forever on a consent screen nobody is watching.
    """
    if not libraries_available():
        raise GmailAuthError(
            "The Google API libraries are missing. Run:\n"
            "  pip install -r requirements.txt"
        )

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception as exc:
            log.warning("Stored Gmail token is unreadable (%s); re-authorising.", exc)
            creds = None

    if creds and creds.valid:
        return creds

    # A refresh token means we can renew without the user present -- this is the
    # path a scheduled run takes, and it is why the browser flow happens once.
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds)
            log.info("Gmail token refreshed.")
            return creds
        except Exception as exc:
            log.warning("Gmail token refresh failed (%s).", exc)
            creds = None

    if not interactive:
        raise GmailAuthError(
            "No valid Gmail token and not running interactively.\n"
            "Run `python auth_gmail.py` once on a machine with a browser."
        )

    if not CLIENT_SECRET_PATH.exists():
        raise GmailAuthError(SETUP_HELP)

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    # port=0 lets the OS pick a free port for the loopback redirect.
    creds = flow.run_local_server(
        port=0,
        prompt="consent",          # force a refresh_token on re-auth
        access_type="offline",     # ...and make it long-lived
        authorization_prompt_message=(
            "\n  Opening your browser to authorise Gmail access.\n"
            "  Sign in as the job-hunt address and approve.\n"
            "  If the browser does not open, visit this URL:\n\n  {url}\n"
        ),
        success_message=(
            "Gmail authorised. You can close this tab and return to the terminal."
        ),
    )
    _save(creds)
    return creds


def _save(creds: Any) -> None:
    _protect_secrets_dir()
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    try:  # best effort; POSIX only
        TOKEN_PATH.chmod(0o600)
    except Exception:
        pass
    log.info("Gmail token stored at %s", TOKEN_PATH)


def gmail_service(interactive: bool = False) -> Any:
    """An authorised Gmail API client."""
    from googleapiclient.discovery import build

    return build(
        "gmail", "v1",
        credentials=load_credentials(interactive=interactive),
        cache_discovery=False,
    )


def authorised_address(interactive: bool = False) -> str:
    """Which mailbox the stored token actually belongs to.

    Worth checking explicitly: authorising the wrong Google account is an easy
    mistake when several are signed in, and it fails later in a confusing way.
    """
    service = gmail_service(interactive=interactive)
    profile = service.users().getProfile(userId="me").execute()
    return str(profile.get("emailAddress", ""))


def revoke() -> bool:
    """Delete the local token. The grant itself is revoked from the Google page."""
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
        log.info("Local Gmail token deleted. Revoke the grant itself at "
                 "https://myaccount.google.com/permissions")
        return True
    return False
