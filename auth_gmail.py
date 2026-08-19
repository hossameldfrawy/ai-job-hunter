"""
One-time Gmail authorisation.

    python auth_gmail.py

Opens a browser, asks you to approve Gmail access for the job-hunt mailbox, and
stores the resulting token in secrets/gmail_token.json. After this the bot
refreshes the token itself; you will not be asked again unless you revoke it.

    python auth_gmail.py --status    what is currently authorised
    python auth_gmail.py --revoke    delete the local token
"""

from __future__ import annotations

import argparse
import sys

from auto_apply.gmail_oauth import (
    CLIENT_SECRET_PATH, SETUP_HELP, TOKEN_PATH, GmailAuthError,
    authorised_address, gmail_service, is_configured, libraries_available,
    revoke,
)
from config import settings

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[1m", "\033[0m"
)
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = BOLD = RESET = ""


def ok(m: str) -> None:
    print(f"  {GREEN}[ OK ]{RESET} {m}")


def fail(m: str) -> None:
    print(f"  {RED}[FAIL]{RESET} {m}")


def warn(m: str) -> None:
    print(f"  {YELLOW}[WARN]{RESET} {m}")


def info(m: str) -> None:
    print(f"  {DIM}       {m}{RESET}")


def show_status() -> int:
    print(f"\n{BOLD}Gmail authorisation status{RESET}")
    print("-" * 44)
    if not libraries_available():
        fail("Google API libraries not installed (pip install -r requirements.txt)")
        return 1
    ok("Google API libraries present")

    print(f"  client secret : {'present' if CLIENT_SECRET_PATH.exists() else 'MISSING'}"
          f"  ({CLIENT_SECRET_PATH.name})")
    print(f"  token         : {'present' if TOKEN_PATH.exists() else 'MISSING'}"
          f"  ({TOKEN_PATH.name})")

    if not is_configured():
        warn("Gmail API not usable yet.")
        if settings.job_email_password:
            info("IMAP fallback IS configured, so the listener will try that.")
        return 1

    try:
        address = authorised_address()
    except GmailAuthError as exc:
        fail(str(exc).splitlines()[0])
        return 1
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        return 1

    ok(f"Authorised as {address}")
    expected = (settings.job_email or "").strip().lower()
    if expected and address.lower() != expected:
        warn(f"That is NOT the configured JOB_EMAIL ({settings.job_email}).")
        info("You approved a different Google account. Re-run with --revoke,")
        info("then authorise again and pick the right one.")
        return 1
    return 0


def authorise() -> int:
    print(f"\n{BOLD}{'=' * 58}")
    print("  GMAIL AUTHORISATION (OAuth2)")
    print(f"{'=' * 58}{RESET}\n")

    if not libraries_available():
        fail("Google API libraries missing.")
        info("pip install -r requirements.txt")
        return 1

    if not CLIENT_SECRET_PATH.exists():
        fail("No OAuth client secret.")
        print()
        print(SETUP_HELP)
        return 1

    if TOKEN_PATH.exists():
        warn("A token already exists; re-authorising will replace it.")
        try:
            if input("  Continue? [y/N]: ").strip().lower() not in ("y", "yes"):
                print("\n  Cancelled.\n")
                return 0
        except (EOFError, KeyboardInterrupt):
            return 130

    print(f"  Sign in as: {BOLD}{settings.job_email or '(JOB_EMAIL not set)'}{RESET}")
    print(f"  {DIM}A consent screen warning about an unverified app is expected --")
    print(f"  it is your own Cloud project. Choose Advanced -> Continue.{RESET}")

    try:
        service = gmail_service(interactive=True)
        address = service.users().getProfile(userId="me").execute().get("emailAddress")
    except GmailAuthError as exc:
        fail(str(exc))
        return 1
    except Exception as exc:
        fail(f"Authorisation failed: {type(exc).__name__}: {exc}")
        return 1

    print()
    ok(f"Authorised as {address}")
    ok(f"Token stored at {TOKEN_PATH}")

    expected = (settings.job_email or "").strip().lower()
    if expected and str(address).lower() != expected:
        warn(f"This is NOT {settings.job_email}.")
        info("Run `python auth_gmail.py --revoke` and try again with the right account.")
        return 1

    print(f"\n{BOLD}  Next:{RESET}  python main.py --inbox\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Authorise Gmail API access.")
    parser.add_argument("--status", action="store_true",
                        help="show what is currently authorised")
    parser.add_argument("--revoke", action="store_true",
                        help="delete the local token")
    args = parser.parse_args()

    if args.status:
        return show_status()
    if args.revoke:
        if revoke():
            ok("Local token deleted.")
            info("Also revoke the grant: https://myaccount.google.com/permissions")
        else:
            info("No local token to delete.")
        return 0

    if not sys.stdin.isatty():
        fail("This needs an interactive terminal (it opens a browser).")
        return 1
    return authorise()


if __name__ == "__main__":
    sys.exit(main())
