"""
One-time Telegram authorisation.

Run this ONCE, interactively, on a machine where you can read the login code
Telegram sends you:

    python auth_telegram.py

It produces two things:

  1. `secrets/job_hunter.session` -- a local session file, used automatically
     whenever you run the bot on this machine.
  2. a TELEGRAM_STRING_SESSION value -- a portable, single-line version of the
     same login, which is what lets GitHub Actions and any cloud worker sign in
     with no human present, forever.

TREAT THE STRING SESSION LIKE A PASSWORD. It is a fully authorised login to
your Telegram account: anyone holding it can read your messages. It goes in
`.env` (git-ignored) and in GitHub Secrets (encrypted) -- never in the repo,
never in a chat, never in a screenshot. If it ever leaks, revoke it from
Telegram: Settings -> Devices -> terminate the "AI Job Hunter" session.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from getpass import getpass
from pathlib import Path

from config import ROOT, settings

SECRETS_DIR = ROOT / "secrets"
SESSION_OUT = SECRETS_DIR / "TELEGRAM_STRING_SESSION.txt"

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[1m", "\033[0m"
)
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = BOLD = RESET = ""


def ok(msg: str) -> None:
    print(f"  {GREEN}[ OK ]{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}[FAIL]{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}[WARN]{RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {DIM}       {msg}{RESET}")


def header(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}\n" + "-" * max(38, len(title)))


def _write_secret_file(session_string: str) -> None:
    SECRETS_DIR.mkdir(exist_ok=True)
    (SECRETS_DIR / ".gitignore").write_text("*\n", encoding="utf-8")
    SESSION_OUT.write_text(session_string, encoding="utf-8")


def _update_env(session_string: str) -> bool:
    """Add/replace TELEGRAM_STRING_SESSION in .env so local runs just work."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return False
    lines = env_path.read_text(encoding="utf-8").splitlines()
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith("TELEGRAM_STRING_SESSION="):
            out.append(f"TELEGRAM_STRING_SESSION={session_string}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"TELEGRAM_STRING_SESSION={session_string}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return True


async def authorise(phone: str | None, force: bool) -> int:
    try:
        from telethon import TelegramClient
        from telethon.errors import (
            FloodWaitError,
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
            PhoneNumberInvalidError,
            SessionPasswordNeededError,
        )
        from telethon.sessions import StringSession
    except ImportError:
        fail("Telethon is not installed.")
        info("Run: pip install -r requirements.txt")
        return 1

    header("1. Credentials")
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        fail("TELEGRAM_API_ID / TELEGRAM_API_HASH are missing from .env")
        info("Get them from https://my.telegram.org -> API development tools")
        return 1
    ok(f"api_id   {settings.telegram_api_id}")
    ok(f"api_hash {settings.telegram_api_hash[:6]}...{settings.telegram_api_hash[-4:]}")

    phone = phone or settings.telegram_phone
    if not phone:
        phone = input("  Telegram phone (international, e.g. +20XXXXXXXXXX): ").strip()
    if not phone.startswith("+"):
        fail(f"Phone must start with '+' and the country code (got {phone!r})")
        return 1
    ok(f"phone    {phone}")

    if settings.telegram_session and not force:
        warn("A TELEGRAM_STRING_SESSION already exists in your environment.")
        info("Re-authorising will create a NEW one and leave the old session")
        info("active on your account until you terminate it in Telegram.")
        if input("  Continue anyway? [y/N]: ").strip().lower() not in ("y", "yes"):
            print("\n  Cancelled -- nothing changed.\n")
            return 0

    # Authorise into the FILE session, then export the portable string from it.
    # One login produces both artifacts -- no second sign-in, no second code.
    session_path = settings.telegram_session_path
    session_path.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(
        str(session_path),
        int(settings.telegram_api_id),
        settings.telegram_api_hash,
        device_model="AI Job Hunter",
        system_version="Windows 11",
        app_version="1.0",
        lang_code="en",
        system_lang_code="en",
    )

    header("2. Login")
    print("  Telegram will send a login code to your Telegram app")
    print("  (check the 'Telegram' service chat), or by SMS.\n")

    await client.connect()
    try:
        try:
            sent = await client.send_code_request(phone)
        except PhoneNumberInvalidError:
            fail(f"Telegram rejected {phone!r} as a phone number.")
            return 1
        except FloodWaitError as exc:
            fail(f"Rate-limited by Telegram. Wait {exc.seconds}s and retry.")
            return 1

        ok(f"Code sent (type: {getattr(sent.type, '__class__', type(sent.type)).__name__})")

        for attempt in range(1, 4):
            code = input(f"\n  Login code (attempt {attempt}/3): ").strip().replace(" ", "")
            if not code:
                continue
            try:
                await client.sign_in(phone=phone, code=code)
                break
            except PhoneCodeInvalidError:
                fail("That code was not correct.")
            except PhoneCodeExpiredError:
                fail("That code expired. Run the script again to get a new one.")
                return 1
            except SessionPasswordNeededError:
                # Two-step verification is on -- ask for the cloud password.
                print()
                ok("Two-step verification is enabled on this account.")
                for pw_attempt in range(1, 4):
                    password = getpass(f"  Telegram password (attempt {pw_attempt}/3): ")
                    try:
                        await client.sign_in(password=password)
                        break
                    except Exception as exc:
                        fail(f"Password rejected: {type(exc).__name__}")
                else:
                    return 1
                break
        else:
            fail("Could not sign in after 3 attempts.")
            return 1

        if not await client.is_user_authorized():
            fail("Sign-in did not complete.")
            return 1

        me = await client.get_me()
        # StringSession.save() accepts any Session instance, which is how the
        # file-backed login is converted into a portable one-line string.
        session_string = StringSession.save(client.session)

        header("3. Signed in")
        ok(f"Account : {getattr(me, 'first_name', '')} "
           f"{getattr(me, 'last_name', '') or ''}".rstrip())
        ok(f"Username: @{getattr(me, 'username', None) or '(none)'}")
        ok(f"User ID : {getattr(me, 'id', '?')}")

        # Count what the bot will be able to see.
        dialogs = groups = channels = 0
        async for dialog in client.iter_dialogs(limit=400):
            dialogs += 1
            if dialog.is_group:
                groups += 1
            elif dialog.is_channel:
                channels += 1
        ok(f"Visible : {dialogs} dialogs -- {groups} groups, {channels} channels")

    finally:
        await client.disconnect()

    # Persist the portable session.
    _write_secret_file(session_string)
    header("4. Session saved")
    ok(f"Local session file  {session_path.with_suffix('.session')}")
    ok(f"Portable session    {SESSION_OUT}")
    if _update_env(session_string):
        ok("TELEGRAM_STRING_SESSION added to .env -- local runs will use it now")
    else:
        warn(".env not found; add this line yourself:")
        info("TELEGRAM_STRING_SESSION=<contents of the file above>")

    header("5. Next steps")
    print("  Verify the connection and list your groups:")
    print(f"    {BOLD}python check_telegram.py{RESET}\n")
    print("  Then push it to the cloud so it runs 24/7 without you:")
    print(f"    {BOLD}gh secret set TELEGRAM_API_ID          --body \"{settings.telegram_api_id}\"{RESET}")
    print(f"    {BOLD}gh secret set TELEGRAM_API_HASH        --body \"{settings.telegram_api_hash}\"{RESET}")
    print(f"    {BOLD}gh secret set TELEGRAM_STRING_SESSION  < secrets/TELEGRAM_STRING_SESSION.txt{RESET}\n")
    warn("That string is a full login to your account. Keep it in secrets only.")
    info("Revoke any time: Telegram -> Settings -> Devices -> 'AI Job Hunter'")
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-time interactive Telegram authorisation.",
    )
    parser.add_argument("--phone", help="override TELEGRAM_PHONE from .env")
    parser.add_argument("--force", action="store_true",
                        help="re-authorise even if a session already exists")
    args = parser.parse_args()

    print(f"\n{BOLD}{'=' * 60}")
    print("  TELEGRAM USER-CLIENT AUTHORISATION")
    print(f"{'=' * 60}{RESET}")

    if not sys.stdin.isatty():
        fail("This script needs an interactive terminal to read your login code.")
        info("Run it directly in your terminal, not through a pipe or a task runner.")
        return 1

    try:
        return asyncio.run(authorise(args.phone, args.force))
    except KeyboardInterrupt:
        print("\n  Cancelled.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
