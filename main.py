"""
AI Job Hunter -- entry point.

    python main.py                 one hunt, then exit  (GitHub Actions / cron)
    python main.py --daemon        run forever on an interval (Docker / Render)
    python main.py --live          real-time Telegram listener + periodic sweeps
    python main.py --dry-run       full pipeline, but print alerts instead of
                                   sending them
    python main.py --stats         what the bot has done so far
    python main.py --selftest      prove Gemini + WhatsApp are reachable
    python main.py --prune         compact the dedup database

`--once` is the cloud default: the process is expected to be killed and
recreated by the scheduler, and ALL state lives in the SQLite file, so a run is
completely stateless apart from that one file.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Any

from config import ConfigError, settings
from db import Database
from notifier import WhatsAppNotifier
from pipeline import handle_live_job, run_once

log = logging.getLogger("job_hunter")

_shutdown = threading.Event()


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(stream)
    # These are noisy and never useful here.
    for noisy in ("urllib3", "charset_normalizer", "telethon", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _banner() -> None:
    log.info("=" * 68)
    log.info("  AI JOB HUNTER -- autonomous job discovery + WhatsApp alerts")
    log.info("  threshold=%d%%  phone=%s  dry_run=%s",
             settings.match_threshold,
             settings.whatsapp_phone[:6] + "***" if settings.whatsapp_phone else "unset",
             settings.dry_run)
    log.info("=" * 68)


# ---------------------------------------------------------------------------
# Health endpoint -- lets Render/Railway/Fly see the worker as alive
# ---------------------------------------------------------------------------
def start_health_server(state: dict[str, Any]) -> None:
    port = os.environ.get("PORT")
    if not port:
        return

    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            body = json.dumps(state, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass  # keep health pings out of the application log

    def serve() -> None:
        try:
            HTTPServer(("0.0.0.0", int(port)), Handler).serve_forever()
        except Exception as exc:
            log.warning("Health server could not start on port %s: %s", port, exc)

    threading.Thread(target=serve, daemon=True, name="health").start()
    log.info("Health endpoint listening on :%s", port)


def _install_signal_handlers() -> None:
    def handle(signum: int, _frame: Any) -> None:
        log.info("Signal %s received -- finishing the current step, then exiting.", signum)
        _shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handle)
        except (ValueError, OSError):
            pass  # not available on every platform/thread


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_once(db: Database) -> int:
    report = run_once(db)
    print("\n" + json.dumps(report.to_dict(), indent=2, ensure_ascii=False)[:4000])
    return 0 if report.status != "fatal" else 1


def cmd_daemon(db: Database, interval_minutes: int) -> int:
    state: dict[str, Any] = {"status": "starting", "runs": 0, "last_report": None}
    start_health_server(state)
    _install_signal_handlers()

    log.info("Daemon mode: hunting every %d minute(s). Ctrl-C to stop.", interval_minutes)
    while not _shutdown.is_set():
        try:
            report = run_once(db)
            state.update(
                status="ok" if report.status == "ok" else report.status,
                runs=state["runs"] + 1,
                last_report=report.to_dict(),
            )
        except Exception as exc:
            log.exception("Unhandled error in the hunt loop: %s", exc)
            state["status"] = "error"

        # Reset per-run HTTP circuit breakers so a host blocked last cycle
        # gets a clean chance this cycle.
        import http_client

        http_client.reset_circuits()

        for _ in range(interval_minutes * 60):
            if _shutdown.is_set():
                break
            time.sleep(1)

    log.info("Daemon stopped cleanly after %d run(s).", state["runs"])
    return 0


def cmd_live(db: Database, interval_minutes: int) -> int:
    """Real-time mode: a persistent Telegram listener plus periodic full sweeps.

    The listener reacts the instant a message lands in one of your groups, so a
    high-scoring post reaches WhatsApp in seconds. The periodic sweep still runs
    underneath it, because every other source (LinkedIn, talent.com, the job
    APIs, RSS) has no push channel and must be polled.

    This mode needs a process that stays alive, so it is for Docker, a VPS or
    your own machine -- NOT for GitHub Actions, whose runs are killed on
    completion. Scheduled runs use poll mode, which covers the same chats.
    """
    from cv_profile import load_cv
    from evaluator import GeminiEvaluator
    from scrapers.telegram_user_client import TelegramLiveListener

    if not settings.telegram_ready:
        log.error(
            "Live mode needs the Telegram user client. Run `python auth_telegram.py` "
            "once, then try again."
        )
        return 2

    cfg = settings.source("telegram_user")
    if not settings.source_enabled("telegram_user"):
        log.error("telegram_user is disabled in config.yml; nothing to listen to.")
        return 2

    state: dict[str, Any] = {"status": "starting", "runs": 0, "live_alerts": 0}
    start_health_server(state)
    _install_signal_handlers()

    cv = load_cv()
    # One shared notifier keeps the CallMeBot send-throttle honest across both
    # the listener thread and the sweep loop.
    notifier = WhatsAppNotifier(db)
    evaluator = GeminiEvaluator()

    def on_job(job: Any) -> None:
        if handle_live_job(job, db, notifier, cv.to_prompt(), evaluator):
            state["live_alerts"] = state.get("live_alerts", 0) + 1

    listener = TelegramLiveListener(cfg, on_job)
    threading.Thread(
        target=listener.run_forever, daemon=True, name="telegram-live"
    ).start()
    log.info("Real-time Telegram listener started.")
    log.info("Periodic sweep of all other sources every %d minute(s).", interval_minutes)

    while not _shutdown.is_set():
        try:
            report = run_once(db, notifier)
            state.update(
                status=report.status,
                runs=state["runs"] + 1,
                last_report=report.to_dict(),
                live_messages_seen=listener.messages_seen,
            )
        except Exception as exc:
            log.exception("Sweep failed: %s", exc)
            state["status"] = "error"

        import http_client

        http_client.reset_circuits()

        for _ in range(interval_minutes * 60):
            if _shutdown.is_set():
                break
            time.sleep(1)

    log.info(
        "Live mode stopped -- %d sweep(s), %d live message(s) seen, %d live alert(s).",
        state["runs"], listener.messages_seen, state.get("live_alerts", 0),
    )
    return 0


def cmd_stats(db: Database) -> int:
    stats = db.stats()
    print("\n" + "=" * 62)
    print("  AI JOB HUNTER -- LIFETIME STATISTICS")
    print("=" * 62)
    print(f"  Postings seen      : {stats['total_jobs_seen']}")
    print(f"  AI-evaluated       : {stats['total_evaluated']}")
    print(f"  WhatsApp alerts    : {stats['total_alerts_sent']}")
    print(f"  Runs completed     : {stats['total_runs']}")
    print(f"  Dedup DB size      : {stats['db_bytes'] / 1024:.1f} KB")

    if stats["jobs_by_source"]:
        print("\n  POSTINGS BY SOURCE")
        for row in stats["jobs_by_source"][:12]:
            print(f"    {row['source']:<28} {row['n']:>6}")

    if stats["top_matches"]:
        print("\n  HIGHEST-SCORING MATCHES")
        for row in stats["top_matches"]:
            print(f"    {row['match_score']:>3}%  {str(row['role_title'])[:42]:<42}"
                  f" @ {str(row['company_name'])[:24]}")

    if stats["recent_runs"]:
        print("\n  RECENT RUNS")
        print(f"    {'when':<22}{'status':<11}{'scrape':>7}{'eval':>6}{'match':>7}{'alert':>7}")
        for row in stats["recent_runs"]:
            print(f"    {str(row['started_at'])[:19]:<22}{str(row['status']):<11}"
                  f"{row['scraped']:>7}{row['evaluated']:>6}{row['matched']:>7}{row['alerted']:>7}")
    print()
    return 0


def cmd_selftest(db: Database) -> int:
    from evaluator import GeminiEvaluator

    print("\n--- SELF TEST ---")
    problems = settings.validate()
    print(f"config      : {'OK' if not problems else 'FAIL -> ' + '; '.join(problems)}")

    try:
        from cv_profile import load_cv

        cv = load_cv()
        print(f"cv          : OK ({cv.chars} chars from {cv.source})")
    except Exception as exc:
        print(f"cv          : FAIL -> {exc}")
        return 1

    ok, detail = GeminiEvaluator().selftest()
    print(f"gemini      : {'OK (' + detail + ')' if ok else 'FAIL -> ' + detail}")

    sent, msg = WhatsAppNotifier(db).selftest()
    print(f"whatsapp    : {'OK (' + msg + ')' if sent else 'FAIL -> ' + msg}")
    print()
    return 0 if ok and sent else 1


def cmd_prune(db: Database, days: int) -> int:
    removed = db.prune(days)
    print(f"Pruned {removed} record(s) older than {days} days.")
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="Autonomous AI job hunter with WhatsApp alerts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="run a single hunt and exit (default)")
    mode.add_argument("--daemon", action="store_true",
                      help="run continuously on an interval")
    mode.add_argument("--live", action="store_true",
                      help="real-time Telegram listener + periodic sweeps")
    mode.add_argument("--stats", action="store_true", help="print lifetime statistics")
    mode.add_argument("--selftest", action="store_true",
                      help="verify Gemini + CallMeBot connectivity")
    mode.add_argument("--prune", action="store_true", help="compact the dedup database")

    p.add_argument("--interval", type=int,
                   default=int(os.environ.get("RUN_INTERVAL_MINUTES", "30")),
                   help="minutes between hunts in --daemon mode (default 30)")
    p.add_argument("--keep-days", type=int, default=180,
                   help="retention window for --prune (default 180)")
    p.add_argument("--dry-run", action="store_true",
                   help="run everything but print alerts instead of sending them")
    p.add_argument("--threshold", type=int,
                   help="override the match threshold for this run only")
    p.add_argument("--log-level", default=settings.log_level,
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)

    if args.dry_run:
        settings.dry_run = True
    if args.threshold is not None:
        settings.raw.setdefault("engine", {})["match_threshold"] = args.threshold

    # --stats never talks to the network, so it must not require credentials.
    if not args.stats:
        try:
            settings.require_valid()
        except ConfigError as exc:
            log.error("%s", exc)
            log.error("Copy .env.example to .env and fill it in, or run "
                      "`python setup_wizard.py`.")
            return 2

    db = Database(settings.db_path)
    try:
        if args.stats:
            return cmd_stats(db)
        if args.selftest:
            return cmd_selftest(db)
        if args.prune:
            return cmd_prune(db, args.keep_days)
        if args.live:
            _banner()
            return cmd_live(db, max(1, args.interval))
        if args.daemon:
            _banner()
            return cmd_daemon(db, max(1, args.interval))
        _banner()
        return cmd_once(db)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
