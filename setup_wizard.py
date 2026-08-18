"""
One-command setup, verification and cloud-activation helper.

    python setup_wizard.py                  full check + live WhatsApp test
    python setup_wizard.py --no-whatsapp    same, but send nothing
    python setup_wizard.py --extract-cv     write the CV text out for secrets
    python setup_wizard.py --telegram-login create a Telethon session string
    python setup_wizard.py --secrets        print the `gh secret set` commands

It verifies each dependency in the order the pipeline needs them and stops
being useful the moment one fails, so the first FAIL you see is the real
problem -- not a cascade.
"""

from __future__ import annotations

import argparse
import base64
import shutil
import subprocess
import sys
from pathlib import Path

from config import ROOT, settings

SECRETS_DIR = ROOT / "secrets"

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[1m", "\033[0m"
)
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = BOLD = RESET = ""

# GitHub caps a single secret at 64 KB; base64 inflates a PDF by ~33%.
GITHUB_SECRET_LIMIT = 64 * 1024


def ok(msg: str) -> None:
    print(f"  {GREEN}[ OK ]{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}[FAIL]{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}[WARN]{RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {DIM}       {msg}{RESET}")


def header(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}\n" + "-" * max(34, len(title)))


# ---------------------------------------------------------------------------
def check_python() -> bool:
    header("1. Python runtime")
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 10):
        fail(f"Python {major}.{minor} found; this project needs 3.10 or newer.")
        return False
    ok(f"Python {major}.{minor}")
    return True


def check_dependencies() -> bool:
    header("2. Dependencies")
    required = {
        "requests": "requests", "bs4": "beautifulsoup4", "lxml": "lxml",
        "feedparser": "feedparser", "yaml": "PyYAML", "tenacity": "tenacity",
    }
    missing = []
    for module, package in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        fail(f"Missing: {', '.join(missing)}")
        info("Fix with:  pip install -r requirements.txt")
        return False
    ok(f"All {len(required)} required packages present")

    if any(__import__("importlib.util", fromlist=["x"]).find_spec(m)
           for m in ("pypdf", "fitz")):
        ok("PDF extraction backend available")
    else:
        warn("No PDF backend (pypdf / PyMuPDF). Needed only if you load a PDF CV.")
    return True


def check_config() -> bool:
    header("3. Credentials")
    problems = settings.validate()
    if problems:
        for p in problems:
            fail(p)
        info("Copy .env.example to .env and fill it in.")
        return False
    ok(f"Gemini API key      ...{settings.gemini_api_key[-6:]}")
    ok(f"CallMeBot API key   {settings.callmebot_apikey}")
    ok(f"WhatsApp number     {settings.whatsapp_phone}")
    return True


def check_cv() -> bool:
    header("4. Master CV")
    try:
        from cv_profile import load_cv

        cv = load_cv(force=True)
    except Exception as exc:
        fail(str(exc))
        return False
    ok(f"Loaded {cv.chars} characters from {cv.source}")
    preview = " ".join(cv.text.split())[:110]
    info(f'"{preview}..."')
    if cv.chars < 500:
        warn("That is very short for a CV -- check the extraction looks right.")
    return True


def check_gemini() -> bool:
    header("5. Gemini AI engine")
    from evaluator import GeminiEvaluator

    evaluator = GeminiEvaluator()
    passed, detail = evaluator.selftest()
    if not passed:
        fail(f"Gemini unreachable: {detail}")
        info("Verify the key at https://aistudio.google.com/apikey")
        return False
    ok(f"Connected, strict-JSON mode confirmed (model: {detail})")

    # A real scoring round-trip catches schema problems a ping cannot.
    from cv_profile import load_cv
    from models import JobPost

    probe = JobPost(
        source="setup_wizard",
        title="VoIP Support Engineer",
        company="Gulf Telecom",
        location="Dubai, UAE",
        url="https://example.com/job/1",
        description="Asterisk/Issabel PBX administration, SIP trunk troubleshooting, IVR design.",
    )
    try:
        result = evaluator.evaluate_batch([probe], load_cv().to_prompt())[0]
        ok(f"Scoring works -- sample VoIP role scored {result.match_score}%")
        info(f"reason: {result.why_matched[:100]}")
    except Exception as exc:
        fail(f"Scoring round-trip failed: {exc}")
        return False
    return True


def check_whatsapp(send: bool) -> bool:
    header("6. WhatsApp delivery (CallMeBot)")
    if not send:
        warn("Skipped (--no-whatsapp).")
        return True

    from db import Database
    from notifier import WhatsAppNotifier

    db = Database(settings.db_path)
    try:
        was_dry, settings.dry_run = settings.dry_run, False
        sent, detail = WhatsAppNotifier(db).selftest()
        settings.dry_run = was_dry
    finally:
        db.close()

    if not sent:
        fail(f"CallMeBot rejected the message: {detail}")
        info("Activation: WhatsApp '+34 644 51 95 23' with the exact text")
        info("'I allow callmebot to send me messages', then use the key it returns.")
        return False
    ok(f"Test message queued to {settings.whatsapp_phone}")
    info("Check your phone -- it usually lands within ~10 seconds.")
    return True


def check_database() -> bool:
    header("7. Deduplication store")
    from db import Database

    db = Database(settings.db_path)
    try:
        stats = db.stats()
        ok(f"SQLite ready at {settings.db_path}")
        info(f"{stats['total_jobs_seen']} postings tracked, "
             f"{stats['total_alerts_sent']} alerts sent to date")
    finally:
        db.close()
    return True


def check_sources() -> bool:
    header("8. Ingestion sources")
    import scrapers

    built = scrapers.build_scrapers(settings)
    if not built:
        fail("No sources enabled in config.yml.")
        return False
    ok(f"{len(built)} source(s) enabled: {', '.join(s.name for s in built)}")

    telegram_cfg = settings.source("telegram")
    channels = telegram_cfg.get("channels") or []
    if telegram_cfg.get("enabled") and not channels:
        warn("Telegram is on but no channels are listed.")
        info("Validate and add one:  python discover_channels.py @name --add")
    return True


# ---------------------------------------------------------------------------
def export_cv_secret() -> Path | None:
    """Write the CV out in the form the cloud deployment wants."""
    from cv_profile import load_cv

    SECRETS_DIR.mkdir(exist_ok=True)
    (SECRETS_DIR / ".gitignore").write_text("*\n", encoding="utf-8")

    cv = load_cv()
    text_path = SECRETS_DIR / "CV_TEXT.txt"
    text_path.write_text(cv.text, encoding="utf-8")
    ok(f"CV text written to {text_path} ({cv.chars} chars)")

    pdf = Path(settings.cv_path) if settings.cv_path else None
    if pdf and pdf.exists() and pdf.suffix.lower() == ".pdf":
        encoded = base64.b64encode(pdf.read_bytes()).decode("ascii")
        if len(encoded) > GITHUB_SECRET_LIMIT:
            warn(f"Base64 PDF is {len(encoded) / 1024:.0f} KB, over GitHub's "
                 f"{GITHUB_SECRET_LIMIT // 1024} KB secret limit -- use CV_TEXT.")
        else:
            b64_path = SECRETS_DIR / "MASTER_CV_B64.txt"
            b64_path.write_text(encoded, encoding="utf-8")
            ok(f"Base64 PDF written to {b64_path}")
    return text_path


def print_secret_commands() -> None:
    header("Cloud activation -- GitHub Secrets")
    has_gh = shutil.which("gh") is not None
    print(f"\n{DIM}  # Run these once, from inside the repository:{RESET}\n")
    lines = [
        f'gh secret set GEMINI_API_KEY   --body "{settings.gemini_api_key}"',
        f'gh secret set CALLMEBOT_APIKEY --body "{settings.callmebot_apikey}"',
        f'gh secret set WHATSAPP_PHONE   --body "{settings.whatsapp_phone}"',
        'gh secret set CV_TEXT          < secrets/CV_TEXT.txt',
    ]
    for line in lines:
        print(f"    {line}")
    print()
    if not has_gh:
        warn("The GitHub CLI (`gh`) is not installed.")
        info("Either install it, or paste the values into")
        info("Settings -> Secrets and variables -> Actions in your repo.")
    else:
        ok("`gh` is installed -- the commands above will work as-is.")

    print(f"\n{DIM}  # Then enable the schedule:{RESET}\n")
    print("    git add -A && git commit -m 'Deploy AI Job Hunter'")
    print("    git push -u origin main")
    print("    gh workflow run job_hunter.yml     # kick off the first run now")
    print()


def telegram_login() -> int:
    header("Telegram session generator (optional)")
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        fail("Telethon is not installed.")
        info("Run: pip install -r requirements-extra.txt")
        return 1

    print("  Public channels need NONE of this -- it is only for PRIVATE ones.")
    print("  Get api_id / api_hash from https://my.telegram.org -> API development tools\n")
    api_id = input("  api_id   : ").strip()
    api_hash = input("  api_hash : ").strip()
    if not api_id or not api_hash:
        fail("Both values are required.")
        return 1

    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        session = client.session.save()
    print()
    ok("Session created. Store these as secrets (the session IS a login token):")
    print(f"\n    TELEGRAM_API_ID   = {api_id}")
    print(f"    TELEGRAM_API_HASH = {api_hash}")
    print(f"    TELEGRAM_SESSION  = {session}\n")
    return 0


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Set up and verify the AI Job Hunter.")
    parser.add_argument("--no-whatsapp", action="store_true",
                        help="do not send the live test message")
    parser.add_argument("--extract-cv", action="store_true",
                        help="only export the CV for use as a secret")
    parser.add_argument("--telegram-login", action="store_true",
                        help="only generate a Telethon session string")
    parser.add_argument("--secrets", action="store_true",
                        help="only print the cloud activation commands")
    args = parser.parse_args()

    if args.telegram_login:
        return telegram_login()
    if args.extract_cv:
        header("CV export")
        return 0 if export_cv_secret() else 1
    if args.secrets:
        export_cv_secret()
        print_secret_commands()
        return 0

    print(f"\n{BOLD}{'=' * 62}")
    print("  AI JOB HUNTER -- SETUP & VERIFICATION")
    print(f"{'=' * 62}{RESET}")

    steps = [
        ("python", check_python),
        ("dependencies", check_dependencies),
        ("config", check_config),
        ("cv", check_cv),
        ("gemini", check_gemini),
        ("whatsapp", lambda: check_whatsapp(not args.no_whatsapp)),
        ("database", check_database),
        ("sources", check_sources),
    ]

    failed: list[str] = []
    for name, step in steps:
        try:
            if not step():
                failed.append(name)
                # Everything downstream depends on these three.
                if name in ("python", "dependencies", "config"):
                    break
        except Exception as exc:
            fail(f"{name} raised {type(exc).__name__}: {exc}")
            failed.append(name)

    header("Result")
    if failed:
        fail(f"{len(failed)} check(s) failed: {', '.join(failed)}")
        print("\n  Fix the items above, then run this again.\n")
        return 1

    ok("Every check passed. The bot is ready.")
    export_cv_secret()
    print_secret_commands()
    print(f"{BOLD}  Try it locally first:{RESET}")
    print("    python main.py --dry-run     # full pipeline, sends nothing")
    print("    python main.py               # the real thing\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
