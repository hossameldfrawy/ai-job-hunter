"""
Standalone Gmail IMAP authentication diagnostic.

Run it directly:  python test_imap_auth.py

It tries every credential candidate in every plausible surface form, reports
exactly which combination Gmail accepts, and -- on success -- writes the working
value into .env as JOB_EMAIL_APP_PASSWORD.

It is standalone on purpose: it imports nothing from the project, so a
configuration bug in the bot cannot mask or confuse the result. The only thing
being tested here is whether Gmail accepts the credentials.

NOTE: the filename starts with `test_` for readability, but this is a network
diagnostic, not a unit test -- the offline suite lives in tests/ and never
touches a real mailbox. `unittest discover` is scoped to tests/, so this is not
collected.
"""

from __future__ import annotations

import argparse
import imaplib
import re
import ssl
import sys
from pathlib import Path

HOST = "imap.gmail.com"
PORT = 993
USER = "hossam.eldefrawy.dev@gmail.com"

ENV_PATH = Path(__file__).resolve().parent / ".env"


def env_candidates() -> list[tuple[str, str]]:
    """Passwords to try, read from .env rather than written into this file.

    Deliberately NOT hardcoded: this repository is public, and a diagnostic that
    embeds the credential it is diagnosing is a worse problem than the bug it
    was written to find. Pass extra candidates with --password.
    """
    found: list[tuple[str, str]] = []
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            key, _, value = line.strip().partition("=")
            if key.strip() == "JOB_EMAIL_APP_PASSWORD" and value.strip():
                found.append(("from .env", value.strip()))
    return found


def variants(raw: str) -> list[tuple[str, str]]:
    """Every surface form of one app password worth trying.

    Google shows app passwords as four spaced groups. People paste them with the
    spaces, without them, and occasionally with a trailing newline or a
    non-breaking space from the web page. Gmail itself accepts the spaced form,
    but only when the spaces are real U+0020 -- a NBSP fails and looks identical
    on screen, which is the single most common cause of this error.
    """
    cleaned = re.sub(r"[\s ​]+", "", raw)
    spaced = " ".join(cleaned[i:i + 4] for i in range(0, len(cleaned), 4))
    seen: dict[str, str] = {}
    for label, value in (
        ("unspaced", cleaned),
        ("spaced", spaced),
        ("as-given", raw),
        ("as-given stripped", raw.strip()),
    ):
        if value and value not in seen.values():
            seen[label] = value
    return list(seen.items())


def try_login(user: str, password: str, timeout: int = 25) -> tuple[bool, str]:
    """One login attempt. Returns (ok, detail)."""
    conn = None
    try:
        context = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(HOST, PORT, ssl_context=context, timeout=timeout)
        conn.login(user, password)
        status, data = conn.select("INBOX")
        if status != "OK":
            return False, f"login ok but SELECT INBOX failed: {data}"
        total = (data[0] or b"0").decode(errors="replace")
        status, unseen = conn.search(None, "UNSEEN")
        n_unseen = len((unseen[0] or b"").split()) if status == "OK" else -1
        return True, f"INBOX has {total} messages, {n_unseen} unread"
    except imaplib.IMAP4.error as exc:
        return False, f"IMAP: {exc}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


def update_env(password: str) -> bool:
    """Write the verified password into .env, preserving everything else."""
    if not ENV_PATH.exists():
        print(f"  .env not found at {ENV_PATH}; set it by hand.")
        return False

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith("JOB_EMAIL_APP_PASSWORD="):
            out.append(f"JOB_EMAIL_APP_PASSWORD={password}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"JOB_EMAIL_APP_PASSWORD={password}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    return True


def diagnose_failure(details: list[str]) -> None:
    """Explain what a total failure actually means, in likelihood order."""
    blob = " ".join(details).lower()
    print("\n  Every candidate was rejected. What that means:\n")
    if "authenticationfailed" in blob or "invalid credentials" in blob:
        print("  Gmail reached the auth stage and said no, so the network, TLS and")
        print("  IMAP service are all fine. The credential itself is the problem:\n")
        print("   1. 2-Step Verification must be ON for the account. App passwords")
        print("      do not exist without it, and any shown before it was enabled")
        print("      stop working.")
        print("   2. App passwords are revoked whenever the account password")
        print("      changes -- including a reset you did not initiate.")
        print("   3. The password must belong to THIS mailbox. One generated on a")
        print("      different Google account fails with exactly this error.")
        print("   4. A brand-new Google account can need a normal browser sign-in")
        print("      before IMAP is permitted at all.")
        print("\n   Generate a fresh one: https://myaccount.google.com/apppasswords")
    elif "gaierror" in blob or "timed out" in blob or "refused" in blob:
        print("  The failure is a NETWORK one, not a credential one -- imap.gmail.com")
        print("  was never reached. Check the connection, a firewall, or a VPN.")
    else:
        print("  Unrecognised failure mode; the raw errors are above.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=USER)
    parser.add_argument("--password", action="append", default=[],
                        help="extra candidate to try (repeatable)")
    parser.add_argument("--no-write", action="store_true",
                        help="do not update .env on success")
    args = parser.parse_args()

    candidates = env_candidates()
    candidates += [(f"cli-{i + 1}", p) for i, p in enumerate(args.password)]
    if not candidates:
        print()
        print("  No candidates. Put one in .env as JOB_EMAIL_APP_PASSWORD, or:")
        print("    python test_imap_auth.py --password xxxxxxxxxxxxxxxx")
        print()
        return 2

    print(f"\n  Gmail IMAP diagnostic")
    print(f"  host : {HOST}:{PORT} (SSL)")
    print(f"  user : {args.user}")
    print(f"  {len(candidates)} candidate password(s)\n")
    print(f"  {'CANDIDATE':<12}{'FORM':<20}{'LEN':<5}RESULT")
    print("  " + "-" * 72)

    failures: list[str] = []
    for name, raw in candidates:
        for form, value in variants(raw):
            ok, detail = try_login(args.user, value)
            status = "OK   " if ok else "FAIL "
            print(f"  {name:<12}{form:<20}{len(value):<5}{status}{detail[:44]}")
            if ok:
                print(f"\n  SUCCESS: candidate {name!r} in {form!r} form.")
                if args.no_write:
                    print("  (--no-write: .env untouched)")
                elif update_env(value):
                    print(f"  .env updated: JOB_EMAIL_APP_PASSWORD={'*' * len(value)}")
                print()
                return 0
            failures.append(detail)

    diagnose_failure(failures)
    print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
