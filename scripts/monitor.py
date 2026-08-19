"""
Mission control: one screen that says whether the bot is actually working.

WHAT THIS IS FOR
----------------
Every failure this system has had in production was SILENT. The review card
still arrived, the reply still got typed, and nothing happened -- because the
listener had died, or a board had logged the browser out, or a Telegram reply
was failing while WhatsApp went out fine and the log said "delivered". None of
those announce themselves. You find out days later, from the absence of an
interview.

So this dashboard is built around one question: *is each moving part alive
right now?* Sessions, channels, processes and counters, refreshed on a timer,
next to the live log so a claim on the status line can be checked against what
the system is actually saying.

STRICTLY READ-ONLY, AND THAT IS A DESIGN CONSTRAINT
---------------------------------------------------
The databases are opened with `mode=ro`, which SQLite enforces -- not by
convention, by refusing writes. A monitor must not be able to damage or lock
the thing it monitors, and `db.Database()` would run migrations and take write
locks against a daemon mid-run. Nothing here writes, sends, or touches the
network.

Run:  python scripts/monitor.py
      python scripts/monitor.py --once      one snapshot, no live loop
      python scripts/monitor.py --plain     no colour (logs, CI, redirection)
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows consoles default to cp1252/cp437 and destroy the Arabic that half
# these logs are written in. Same fix config.py applies to the bot itself.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

JOBS_DB = ROOT / "state" / "jobs.db"
VAULT_DB = ROOT / "state" / "vault.db"
LOG_FILES = {
    "listener": ROOT / "state" / "listener.log",
    "hunter": ROOT / "state" / "hunter.log",
}

#: The boards whose saved login the dashboard reports on.
WATCHED_BOARDS = ("Tanqeeb", "Wuzzuf", "Talent.com", "GulfTalent", "Bayt")

APPLY_THRESHOLD = 80


# ---------------------------------------------------------------------------
# Read-only data access
# ---------------------------------------------------------------------------
def _read_only(path: Path) -> sqlite3.Connection | None:
    """Open a database that CANNOT be written to. None if it is not there yet.

    `mode=ro` is enforced by SQLite itself, so no bug in this file can corrupt
    the state the bot depends on, and no query here can take a write lock away
    from a daemon that is mid-run.
    """
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True,
                              timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _scalar(conn: sqlite3.Connection | None, query: str, default: int = 0) -> int:
    if conn is None:
        return default
    try:
        row = conn.execute(query).fetchone()
        return int(row[0]) if row and row[0] is not None else default
    except sqlite3.Error:
        return default


@dataclass(slots=True)
class Kpis:
    scanned: int = 0
    evaluated: int = 0
    matches: int = 0
    drafts_pending: int = 0
    submitted: int = 0
    failed: int = 0
    alerts_sent: int = 0
    interviews: int = 0
    runs: int = 0
    last_run: str = ""
    last_run_status: str = ""

    def as_row(self) -> list[tuple[str, str, str]]:
        """(label, value, emphasis) for the KPI strip."""
        return [
            ("Scanned", f"{self.scanned:,}", "dim"),
            ("Evaluated", f"{self.evaluated:,}", "dim"),
            (f"Matches ≥{APPLY_THRESHOLD}%", str(self.matches), "good"),
            ("Drafts pending", str(self.drafts_pending),
             "warn" if self.drafts_pending else "dim"),
            ("Submitted", str(self.submitted),
             "good" if self.submitted else "dim"),
            ("Failed", str(self.failed), "bad" if self.failed else "dim"),
            ("Interviews", str(self.interviews),
             "good" if self.interviews else "dim"),
        ]


def collect_kpis() -> Kpis:
    """Every counter on the panel, from the two databases. Never raises."""
    kpis = Kpis()
    jobs = _read_only(JOBS_DB)
    if jobs is not None:
        try:
            kpis.scanned = _scalar(jobs, "SELECT COUNT(*) FROM seen_jobs")
            kpis.evaluated = _scalar(
                jobs, "SELECT COUNT(*) FROM seen_jobs WHERE evaluated=1")
            kpis.matches = _scalar(
                jobs,
                f"SELECT COUNT(*) FROM evaluations "
                f"WHERE match_score >= {APPLY_THRESHOLD}")
            kpis.alerts_sent = _scalar(
                jobs, "SELECT COUNT(*) FROM alerts WHERE status='sent'")
            kpis.runs = _scalar(jobs, "SELECT COUNT(*) FROM runs")
            try:
                row = jobs.execute(
                    "SELECT started_at, status FROM runs "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row:
                    kpis.last_run = str(row["started_at"] or "")[:19]
                    kpis.last_run_status = str(row["status"] or "")
            except sqlite3.Error:
                pass
        finally:
            jobs.close()

    vault = _read_only(VAULT_DB)
    if vault is not None:
        try:
            by_status: dict[str, int] = {}
            try:
                by_status = {
                    str(r["status"]): int(r["n"]) for r in vault.execute(
                        "SELECT status, COUNT(*) AS n "
                        "FROM applications_history GROUP BY status")
                }
            except sqlite3.Error:
                pass
            # `review_pending` and `approved` are both "waiting on you".
            kpis.drafts_pending = (by_status.get("review_pending", 0)
                                   + by_status.get("approved", 0)
                                   + by_status.get("draft", 0))
            kpis.submitted = by_status.get("submitted", 0)
            kpis.failed = by_status.get("failed", 0)
            kpis.interviews = _scalar(
                vault,
                "SELECT COUNT(*) FROM email_interview_events "
                "WHERE classification IN ('interview','assessment',"
                "'recruiter_outreach')")
        finally:
            vault.close()
    return kpis


def pending_drafts(limit: int = 6) -> list[dict[str, Any]]:
    """The drafts actually waiting on a reply, so the screen names them."""
    vault = _read_only(VAULT_DB)
    if vault is None:
        return []
    try:
        return [dict(r) for r in vault.execute(
            "SELECT id, company, role, status FROM applications_history "
            "WHERE status IN ('review_pending','approved','draft') "
            "ORDER BY id DESC LIMIT ?", (int(limit),))]
    except sqlite3.Error:
        return []
    finally:
        vault.close()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Health:
    label: str
    ok: bool
    detail: str = ""


def session_health() -> list[Health]:
    """Which boards we hold a signed-in browser session for.

    This is the single most load-bearing status on the screen. Without a saved
    login these boards serve a public landing page whose only form is the site
    search, the submit gate (correctly) refuses it, and every approval fails
    with what looks like a mysterious form error.
    """
    try:
        from auto_apply.browser import session_status
        from auto_apply.profile_builder import find_platform
    except Exception as exc:
        return [Health("sessions", False, f"unavailable: {exc}")]

    out: list[Health] = []
    for name in WATCHED_BOARDS:
        platform = find_platform(name)
        slug = platform.slug if platform else name.lower()
        hosts = platform.hosts if platform else ()
        try:
            ok, detail = session_status(slug, hosts)
        except Exception as exc:
            ok, detail = False, f"unreadable: {exc}"[:48]
        out.append(Health(name, ok, detail))
    return out


def _running_processes() -> list[str]:
    """Command lines of the python processes we might have started."""
    try:
        import psutil
    except Exception:
        return []
    found: list[str] = []
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (process.info.get("name") or "").lower()
            if "python" not in name:
                continue
            cmdline = " ".join(process.info.get("cmdline") or [])
            if "main.py" in cmdline:
                found.append(cmdline)
        except Exception:
            continue
    return found


def channel_health(processes: Iterable[str] | None = None) -> list[Health]:
    """Are the inbound and outbound channels actually up?"""
    lines = list(processes) if processes is not None else _running_processes()
    out: list[Health] = []

    listening = any("--listen" in c for c in lines)
    owner = ""
    try:
        from auto_apply.control import hitl_cfg

        owner = str(hitl_cfg().get("telegram_owner_id") or "")
    except Exception:
        pass
    out.append(Health(
        "Telegram listener", listening,
        f"active (owner {owner})" if listening and owner
        else "active" if listening
        else "NOT RUNNING — replies will be ignored",
    ))

    try:
        from auto_apply import inbound

        ready, why = inbound.readiness()
        out.append(Health("WhatsApp inbound", ready,
                          "webhook live" if ready else why.split(".")[0]))
    except Exception as exc:
        out.append(Health("WhatsApp inbound", False, str(exc)[:60]))

    try:
        from config import settings

        configured = bool(settings.whatsapp_phone and settings.callmebot_apikey)
        out.append(Health("WhatsApp outbound", configured,
                          "CallMeBot configured" if configured
                          else "CALLMEBOT_APIKEY / WHATSAPP_PHONE missing"))
    except Exception as exc:
        out.append(Health("WhatsApp outbound", False, str(exc)[:60]))

    try:
        from auto_apply import gmail_oauth

        ok = gmail_oauth.is_configured()
        out.append(Health("Gmail OAuth2 inbox", ok,
                          "token present" if ok
                          else "run: python auth_gmail.py"))
    except Exception as exc:
        out.append(Health("Gmail OAuth2 inbox", False, str(exc)[:60]))

    return out


def hunter_mode(processes: Iterable[str] | None = None) -> tuple[str, bool]:
    """(description, running) for the discovery half of the system."""
    lines = list(processes) if processes is not None else _running_processes()
    for cmdline in lines:
        if "--daemon" in cmdline:
            interval = "?"
            parts = cmdline.split()
            if "--interval" in parts:
                try:
                    interval = parts[parts.index("--interval") + 1]
                except IndexError:
                    pass
            return f"Autonomous daemon — every {interval} min", True
        if "--live" in cmdline:
            return "Live Telegram listener + periodic sweeps", True
    return "Not running — start it from Launch_Job_Hunter.bat", False


# ---------------------------------------------------------------------------
# Live log
# ---------------------------------------------------------------------------
class LogTail:
    """Follows several UTF-8 log files at once, newest lines last.

    Reads incrementally from a remembered byte offset and, when a file gets
    SMALLER, starts over -- the supervisor rotates a log it finds in the wrong
    encoding, and a tail that kept its old offset would then read from the
    middle of a line forever.
    """

    def __init__(self, paths: dict[str, Path], keep: int = 400) -> None:
        self.paths = paths
        self.keep = keep
        self.offsets: dict[str, int] = {}
        self.lines: list[tuple[str, str]] = []

    def prime(self, tail_bytes: int = 8000) -> None:
        """Start near the end, so the first frame is not the whole history."""
        for name, path in self.paths.items():
            try:
                size = path.stat().st_size
            except OSError:
                continue
            self.offsets[name] = max(0, size - tail_bytes)
        self.poll()

    def poll(self) -> list[tuple[str, str]]:
        """Read whatever is new. Returns the lines added this call."""
        added: list[tuple[str, str]] = []
        for name, path in self.paths.items():
            try:
                size = path.stat().st_size
            except OSError:
                continue
            offset = self.offsets.get(name, 0)
            if size < offset:            # truncated or rotated
                offset = 0
            if size == offset:
                continue
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read(size - offset)
                self.offsets[name] = size
            except OSError:
                continue
            # `Add-Content -Encoding utf8` on PowerShell 5.1 writes a BOM,
            # which decodes to U+FEFF and renders as a stray glyph mid-line.
            text = chunk.decode("utf-8", errors="replace").replace("﻿", "")
            for line in text.splitlines():
                line = line.rstrip()
                if line:
                    added.append((name, line))
        if added:
            self.lines.extend(added)
            del self.lines[:-self.keep]
        return added


#: How a log line is coloured, most specific first.
LINE_STYLES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("SUBMITTED", "submitted", "APPLICATION SUCCESSFULLY"), "good"),
    (("ERROR", "FAILED", "Refusing", "refused", "CRITICAL"), "bad"),
    (("WARNING", "warn", "quota", "flood"), "warn"),
    (("DRAFT", "drafted", "review"), "accent"),
    (("done", "edit ", "موافق", "تعديل"), "accent"),
)


def style_for(line: str) -> str:
    for needles, style in LINE_STYLES:
        if any(n in line for n in needles):
            return style
    return "dim"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
PALETTE = {
    "good": "\033[92m", "bad": "\033[91m", "warn": "\033[93m",
    "accent": "\033[96m", "dim": "\033[90m", "head": "\033[1;97m",
    "reset": "\033[0m",
}


def _mark(ok: bool) -> str:
    return "●" if ok else "○"


def render_plain(kpis: Kpis, sessions: list[Health], channels: list[Health],
                 mode: tuple[str, bool], log_lines: list[tuple[str, str]],
                 colour: bool = True, rows: int = 18) -> str:
    """The whole screen as one string. Used for --plain, --once and tests."""
    def paint(text: str, style: str) -> str:
        if not colour:
            return text
        return f"{PALETTE.get(style, '')}{text}{PALETTE['reset']}"

    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    width = 78
    out: list[str] = []
    out.append(paint("═" * width, "head"))
    out.append(paint(f" AI JOB HUNTER — MISSION CONTROL{' ' * 16}{stamp}", "head"))
    out.append(paint("═" * width, "head"))

    out.append(f" MODE      {paint(mode[0], 'good' if mode[1] else 'warn')}")
    if kpis.last_run:
        out.append(f" LAST RUN  {kpis.last_run}  ({kpis.last_run_status})")

    out.append("")
    out.append(paint(" SESSIONS", "head"))
    for health in sessions:
        out.append(f"   {paint(_mark(health.ok), 'good' if health.ok else 'bad')} "
                   f"{health.label:<14}{paint(health.detail, 'dim')}")

    out.append("")
    out.append(paint(" CHANNELS", "head"))
    for health in channels:
        out.append(f"   {paint(_mark(health.ok), 'good' if health.ok else 'bad')} "
                   f"{health.label:<20}{paint(health.detail, 'dim')}")

    out.append("")
    out.append(paint(" METRICS", "head"))
    cells = [f"{label} {paint(value, style)}"
             for label, value, style in kpis.as_row()]
    out.append("   " + "  |  ".join(cells))

    waiting = pending_drafts()
    if waiting:
        out.append("")
        out.append(paint(" WAITING ON YOU", "head"))
        for row in waiting:
            out.append(
                f"   [DRAFT #{row['id']}] {str(row.get('company'))[:24]:<24} "
                f"{str(row.get('role'))[:32]:<32} {paint(str(row.get('status')), 'warn')}"
            )
        out.append(paint("   reply  done <id>  /  edit <id> salary: …  "
                         "/  موافق <id>", "dim"))

    out.append("")
    out.append(paint(" LIVE ACTIVITY", "head"))
    if not log_lines:
        out.append(paint("   (no log output yet — "
                         "state/listener.log, state/hunter.log)", "dim"))
    for source, line in log_lines[-rows:]:
        out.append(f"   {paint(f'{source:<9}', 'dim')}{paint(line[:64], style_for(line))}")
    return "\n".join(out)


def _rich_available() -> bool:
    try:
        import rich  # noqa: F401

        return True
    except Exception:
        return False


def run_rich(interval: float, rows: int) -> int:
    """The live dashboard. Falls back to the plain renderer if rich is absent."""
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    console = Console()
    tail = LogTail(LOG_FILES)
    tail.prime()

    styles = {"good": "bold green", "bad": "bold red", "warn": "yellow",
              "accent": "cyan", "dim": "grey58", "head": "bold white"}

    def frame() -> Group:
        tail.poll()
        processes = _running_processes()
        kpis = collect_kpis()
        mode, running = hunter_mode(processes)

        header = Table.grid(expand=True)
        header.add_column(justify="left")
        header.add_column(justify="right")
        header.add_row(
            Text("AI JOB HUNTER — MISSION CONTROL", style="bold white"),
            Text(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), style="grey58"),
        )
        header.add_row(
            Text(f"MODE  {mode}", style="bold green" if running else "yellow"),
            Text(f"last run {kpis.last_run or 'never'} "
                 f"({kpis.last_run_status or '-'})", style="grey58"),
        )

        status = Table.grid(expand=True, padding=(0, 2))
        status.add_column(ratio=1)
        status.add_column(ratio=1)

        sessions_table = Table.grid(padding=(0, 1))
        sessions_table.add_column(width=2)
        sessions_table.add_column()
        for health in session_health():
            sessions_table.add_row(
                Text(_mark(health.ok), style=styles["good" if health.ok else "bad"]),
                Text(f"{health.label:<12}{health.detail}", style="grey58"),
            )

        channels_table = Table.grid(padding=(0, 1))
        channels_table.add_column(width=2)
        channels_table.add_column()
        for health in channel_health(processes):
            channels_table.add_row(
                Text(_mark(health.ok), style=styles["good" if health.ok else "bad"]),
                Text(f"{health.label:<19}{health.detail}", style="grey58"),
            )

        status.add_row(
            Panel(sessions_table, title="Saved board sessions",
                  border_style="grey35"),
            Panel(channels_table, title="Channels", border_style="grey35"),
        )

        metrics = Table.grid(expand=True, padding=(0, 3))
        for _ in kpis.as_row():
            metrics.add_column(justify="center")
        metrics.add_row(*[Text(label, style="grey58")
                          for label, _v, _s in kpis.as_row()])
        metrics.add_row(*[Text(value, style=styles.get(style, "white"))
                          for _l, value, style in kpis.as_row()])

        blocks = [header, status,
                  Panel(metrics, title="Metrics", border_style="grey35")]

        waiting = pending_drafts()
        if waiting:
            table = Table.grid(padding=(0, 2))
            table.add_column()
            table.add_column()
            table.add_column()
            for row in waiting:
                table.add_row(
                    Text(f"[DRAFT #{row['id']}]", style="cyan"),
                    Text(f"{str(row.get('company'))[:26]} — "
                         f"{str(row.get('role'))[:30]}", style="white"),
                    Text(str(row.get("status")), style="yellow"),
                )
            table.add_row(Text(""), Text(
                "reply:  done <id>   |   edit <id> salary: …   |   موافق <id>",
                style="grey58"), Text(""))
            blocks.append(Panel(table, title="Waiting on you",
                                border_style="yellow"))

        activity = Table.grid(padding=(0, 1))
        activity.add_column(width=9)
        activity.add_column(overflow="ellipsis")
        recent = tail.lines[-rows:]
        if not recent:
            activity.add_row(Text("—", style="grey58"),
                             Text("no log output yet", style="grey58"))
        for source, line in recent:
            activity.add_row(
                Text(source, style="grey58"),
                Text(line, style=styles.get(style_for(line), "grey58")),
            )
        blocks.append(Panel(activity, title="Live activity (UTF-8)",
                            border_style="grey35"))
        return Group(*blocks)

    console.print("[grey58]Ctrl-C to close the dashboard. "
                  "This does not stop the bot.[/]")
    try:
        with Live(frame(), console=console, refresh_per_second=4,
                  screen=False) as live:
            while True:
                time.sleep(interval)
                live.update(frame())
    except KeyboardInterrupt:
        console.print("\n[grey58]Dashboard closed. The bot is still "
                      "running — stop it with scripts\\stop_hunter.ps1[/]")
    return 0


def run_plain(interval: float, rows: int, colour: bool, once: bool) -> int:
    tail = LogTail(LOG_FILES)
    tail.prime()
    try:
        while True:
            tail.poll()
            processes = _running_processes()
            screen = render_plain(
                collect_kpis(), session_health(), channel_health(processes),
                hunter_mode(processes), tail.lines, colour=colour, rows=rows,
            )
            if not once:
                os.system("cls" if os.name == "nt" else "clear")
            print(screen, flush=True)
            if once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nDashboard closed. The bot is still running.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="monitor.py",
        description="Live mission-control dashboard for the AI Job Hunter. "
                    "Read-only: it cannot change or stop the bot.",
    )
    parser.add_argument("--once", action="store_true",
                        help="print one snapshot and exit")
    parser.add_argument("--plain", action="store_true",
                        help="ANSI instead of rich (redirection, CI, logs)")
    parser.add_argument("--no-colour", action="store_true",
                        help="no ANSI colour at all")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="seconds between refreshes (default 2)")
    parser.add_argument("--rows", type=int, default=16,
                        help="log lines to show (default 16)")
    args = parser.parse_args(argv)

    if args.once or args.plain or args.no_colour or not _rich_available():
        return run_plain(args.interval, args.rows,
                        colour=not args.no_colour, once=args.once)
    return run_rich(args.interval, args.rows)


if __name__ == "__main__":
    sys.exit(main())
