"""
Pre-flight: is this machine actually able to run the bot?

Called by Launch_Job_Hunter.bat before anything is started, because the
alternative is a launcher that reports success and a bot that quietly does
nothing. Every check here maps to a failure that is otherwise SILENT:

  * no Telethon / no session  -> the listener starts, attaches to nothing, and
                                 your "done 7" is never seen
  * no Playwright browser     -> drafting throws inside a thread and the run
                                 looks like "no matches today"
  * no CV on disk             -> applications submit without an attachment and
                                 are rejected by the board, not by us
  * no Gemini key             -> every evaluation scores 0 and nothing matches

Exit codes:  0 = ready   1 = ready with warnings   2 = cannot run
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

OK, WARN, FAIL = "[ok]", "[!]", "[X]"

#: (import name, human name, fatal?) -- fatal means the bot cannot run at all.
PACKAGES: tuple[tuple[str, str, bool], ...] = (
    ("requests", "requests", True),
    ("yaml", "PyYAML", True),
    ("bs4", "beautifulsoup4", True),
    ("feedparser", "feedparser", True),
    ("cryptography", "cryptography (vault encryption)", True),
    ("telethon", "Telethon (Telegram listener)", True),
    ("playwright", "Playwright (auto-apply)", False),
    ("rich", "rich (dashboard)", False),
    ("psutil", "psutil (process health)", False),
    ("googleapiclient", "google-api-python-client (Gmail)", False),
)


def _chromium_installed() -> bool:
    """Is a Chromium build present in Playwright's browser cache?"""
    import os

    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    candidates = []
    if override and override != "0":
        candidates.append(Path(override))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "ms-playwright")
    candidates.append(Path.home() / "AppData" / "Local" / "ms-playwright")
    candidates.append(Path.home() / ".cache" / "ms-playwright")

    for base in candidates:
        try:
            if not base.is_dir():
                continue
            for build in base.glob("chromium*"):
                if any(build.rglob("chrome.exe")) or any(build.rglob("chrome")):
                    return True
        except OSError:
            continue
    return False


def main() -> int:
    problems = 0
    warnings = 0

    print("   Checking dependencies...")
    for module, label, fatal in PACKAGES:
        try:
            importlib.import_module(module)
            print(f"   {OK} {label}")
        except Exception:
            if fatal:
                print(f"   {FAIL} {label} -- MISSING (required)")
                problems += 1
            else:
                print(f"   {WARN} {label} -- missing (optional)")
                warnings += 1

    print("   Checking configuration...")
    try:
        from config import settings

        for label, present, fatal in (
            ("GEMINI_API_KEY", bool(settings.gemini_api_key), True),
            ("CALLMEBOT_APIKEY", bool(settings.callmebot_apikey), True),
            ("WHATSAPP_PHONE", bool(settings.whatsapp_phone), True),
            ("Telegram session", settings.telegram_ready, True),
        ):
            if present:
                print(f"   {OK} {label}")
            elif fatal:
                print(f"   {FAIL} {label} -- not configured")
                problems += 1
            else:
                print(f"   {WARN} {label} -- not configured")
                warnings += 1

        cv = next((p for p in settings.cv_paths if p.exists()), None)
        if cv:
            print(f"   {OK} CV found: {cv.name}")
        else:
            print(f"   {WARN} no CV on disk -- applications would submit "
                  f"without an attachment")
            warnings += 1
    except Exception as exc:
        print(f"   {FAIL} configuration could not be loaded: {exc}")
        problems += 1

    # A browser BINARY, not just the Python package. `pip install playwright`
    # succeeds without ever downloading Chromium, and the failure only surfaces
    # much later, inside a worker thread, as "no matches today".
    #
    # Checked by looking in the browser cache rather than by starting
    # Playwright: `sync_playwright()` spins up a Node driver, and tearing it
    # down for a one-line check prints "Task was destroyed but it is pending!"
    # and a TargetClosedError traceback over the launcher's own output.
    if _chromium_installed():
        print(f"   {OK} Chromium installed")
    else:
        print(f"   {WARN} Chromium not found -- run: "
              f"python -m playwright install chromium")
        warnings += 1

    print()
    if problems:
        print(f"   {problems} blocking problem(s). The bot cannot run yet.")
        return 2
    if warnings:
        print(f"   Ready, with {warnings} warning(s) -- some features are off.")
        return 1
    print("   All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
