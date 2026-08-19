"""
Mission control: the read-only guarantee, the health logic, and the log tail.

WHY A MONITOR NEEDS TESTS AT ALL
--------------------------------
Because a status panel that is confidently wrong is worse than no panel. The
whole point of this screen is to answer "is each moving part alive?", and the
two ways it can fail are:

  1. IT DAMAGES WHAT IT WATCHES. `db.Database()` runs migrations and takes
     write locks; pointed at a daemon mid-run that is a real hazard. So the
     dashboard opens everything `mode=ro` -- and that is asserted here by
     trying to write through its own connection and requiring the attempt to
     fail.

  2. IT REPORTS A LOGIN THAT IS NOT A LOGIN. Measured on this machine: the
     saved Talent.com session held six cookies, every one analytics
     (`NEXT_LOCALE`, `statsig.stable_id`, `utm_source`), while Tanqeeb held
     `token` and `user_id`. Judging by "the file exists" called all three
     signed in, which sends someone hunting for a form bug that is really a
     missing login.

Run:  python -m pytest tests/test_dashboard.py -v
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def _load_monitor():
    """Import scripts/monitor.py, which is not on the import path.

    Registered in `sys.modules` BEFORE it is executed. `@dataclass(slots=True)`
    rebuilds the class and looks its module up by name to do so, so a module
    that is not yet registered fails with a bare AttributeError on None.
    """
    name = "hunter_monitor"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "monitor.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


monitor = _load_monitor()


def _seed_jobs_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE seen_jobs (fingerprint TEXT PRIMARY KEY, evaluated INTEGER
            DEFAULT 0, match_score INTEGER, notified INTEGER DEFAULT 0);
        CREATE TABLE evaluations (id INTEGER PRIMARY KEY, match_score INTEGER);
        CREATE TABLE alerts (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at TEXT, status TEXT);
        INSERT INTO seen_jobs VALUES ('a',1,95,1),('b',1,60,0),('c',0,NULL,0);
        INSERT INTO evaluations VALUES (1,95),(2,82),(3,40);
        INSERT INTO alerts VALUES (1,'sent'),(2,'failed'),(3,'sent');
        INSERT INTO runs VALUES (1,'2026-08-19T00:11:22+00:00','ok');
    """)
    conn.commit()
    conn.close()


def _seed_vault_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE applications_history (id INTEGER PRIMARY KEY,
            company TEXT, role TEXT, status TEXT);
        CREATE TABLE email_interview_events (id INTEGER PRIMARY KEY,
            classification TEXT);
        INSERT INTO applications_history VALUES
            (1,'Erada Egypt','IT Help Desk','review_pending'),
            (2,'Konecta','Genesys Admin','failed'),
            (3,'شركة صحرا','فني دعم','review_pending'),
            (4,'Etisalat','VoIP Engineer','submitted');
        INSERT INTO email_interview_events VALUES
            (1,'interview'),(2,'rejection'),(3,'assessment');
    """)
    conn.commit()
    conn.close()


class DashboardHarness(unittest.TestCase):
    """Points the module at throwaway databases for the duration of a test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.jobs = self.tmp / "jobs.db"
        self.vault = self.tmp / "vault.db"
        _seed_jobs_db(self.jobs)
        _seed_vault_db(self.vault)
        self._saved = (monitor.JOBS_DB, monitor.VAULT_DB)
        monitor.JOBS_DB = self.jobs
        monitor.VAULT_DB = self.vault

    def tearDown(self):
        monitor.JOBS_DB, monitor.VAULT_DB = self._saved


# ---------------------------------------------------------------------------
class TestReadOnlyGuarantee(DashboardHarness):
    """A monitor must not be able to damage or lock what it monitors."""

    def test_the_connection_physically_refuses_writes(self):
        conn = monitor._read_only(self.jobs)
        self.assertIsNotNone(conn)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO alerts(status) VALUES ('sent')")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DROP TABLE seen_jobs")
        finally:
            conn.close()

    def test_a_missing_database_is_none_not_a_crash(self):
        self.assertIsNone(monitor._read_only(self.tmp / "nope.db"))

    def test_collecting_kpis_leaves_the_file_byte_identical(self):
        """Opening a database can still mutate it -- migrations, journal
        cleanup. This asserts nothing changed at all."""
        before = self.jobs.read_bytes()
        monitor.collect_kpis()
        monitor.pending_drafts()
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_the_dashboard_never_writes_to_the_vault_either(self):
        before = self.vault.read_bytes()
        monitor.collect_kpis()
        monitor.pending_drafts()
        self.assertEqual(self.vault.read_bytes(), before)

    def test_a_corrupt_database_degrades_rather_than_raising(self):
        (self.tmp / "jobs.db").write_bytes(b"this is not a database")
        kpis = monitor.collect_kpis()
        self.assertEqual(kpis.scanned, 0)


class TestKpis(DashboardHarness):
    def test_the_counters_match_the_data(self):
        kpis = monitor.collect_kpis()
        self.assertEqual(kpis.scanned, 3)
        self.assertEqual(kpis.evaluated, 2)
        self.assertEqual(kpis.matches, 2)        # 95 and 82 are >= 80
        self.assertEqual(kpis.alerts_sent, 2)
        self.assertEqual(kpis.submitted, 1)
        self.assertEqual(kpis.failed, 1)
        self.assertEqual(kpis.interviews, 2)     # interview + assessment

    def test_drafts_pending_counts_everything_waiting_on_a_human(self):
        """`approved` is still waiting: approved-but-not-submitted is exactly
        the state a stalled run leaves behind."""
        conn = sqlite3.connect(str(self.vault))
        conn.execute("INSERT INTO applications_history VALUES "
                     "(5,'X','Y','approved')")
        conn.commit()
        conn.close()
        self.assertEqual(monitor.collect_kpis().drafts_pending, 3)

    def test_the_last_run_is_reported(self):
        kpis = monitor.collect_kpis()
        self.assertTrue(kpis.last_run.startswith("2026-08-19"))
        self.assertEqual(kpis.last_run_status, "ok")

    def test_missing_databases_give_zeroes_not_an_exception(self):
        monitor.JOBS_DB = self.tmp / "gone.db"
        monitor.VAULT_DB = self.tmp / "gone2.db"
        kpis = monitor.collect_kpis()
        self.assertEqual((kpis.scanned, kpis.submitted), (0, 0))
        self.assertEqual(monitor.pending_drafts(), [])

    def test_pending_drafts_names_them_newest_first(self):
        rows = monitor.pending_drafts()
        self.assertEqual([r["id"] for r in rows], [3, 1])

    def test_arabic_company_names_survive(self):
        names = [r["company"] for r in monitor.pending_drafts()]
        self.assertIn("شركة صحرا", names)

    def test_every_kpi_cell_renders(self):
        for label, value, style in monitor.collect_kpis().as_row():
            with self.subTest(label=label):
                self.assertTrue(label)
                self.assertTrue(value)
                self.assertIn(style, {"good", "bad", "warn", "dim", "accent"})


# ---------------------------------------------------------------------------
class TestSessionHealth(unittest.TestCase):
    """"A file exists" is not "you are signed in"."""

    def setUp(self):
        from config import settings

        self.tmp = Path(tempfile.mkdtemp())
        self._saved = dict(settings.raw.get("auto_apply", {}) or {})
        settings.raw["auto_apply"] = dict(self._saved,
                                          session_dir=str(self.tmp))
        self.settings = settings

    def tearDown(self):
        self.settings.raw["auto_apply"] = self._saved

    def _write(self, slug: str, cookies: list[dict]) -> None:
        (self.tmp / f"{slug}_state.json").write_text(
            json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8"
        )

    def test_no_file_is_no_login(self):
        from auto_apply.browser import session_status

        ok, detail = session_status("gulftalent", ("gulftalent.com",))
        self.assertFalse(ok)
        self.assertIn("no saved login", detail)

    def test_analytics_only_cookies_are_not_a_login(self):
        """The real Talent.com session: six cookies, none of them auth."""
        from auto_apply.browser import session_status

        self._write("talent_com", [
            {"name": "NEXT_LOCALE", "domain": "ae.talent.com", "expires": -1},
            {"name": "uet_nuuid", "domain": ".talent.com", "expires": -1},
            {"name": "statsig.stable_id", "domain": "ae.talent.com",
             "expires": -1},
            {"name": "utm_source", "domain": "ae.talent.com", "expires": -1},
        ])
        ok, detail = session_status("talent_com", ("talent.com",))
        self.assertFalse(ok, detail)
        self.assertIn("no auth token", detail)

    def test_an_auth_cookie_is_a_login(self):
        """The real Tanqeeb session: `token` and `user_id` on its own domain."""
        from auto_apply.browser import session_status

        self._write("tanqeeb", [
            {"name": "_ga", "domain": ".tanqeeb.com", "expires": -1},
            {"name": "token", "domain": ".tanqeeb.com", "expires": -1},
            {"name": "user_id", "domain": ".tanqeeb.com", "expires": -1},
        ])
        ok, detail = session_status("tanqeeb", ("tanqeeb.com",))
        self.assertTrue(ok, detail)
        self.assertIn("token", detail)

    def test_third_party_cookies_alone_are_not_a_login(self):
        from auto_apply.browser import session_status

        self._write("wuzzuf", [
            {"name": "session_id", "domain": ".doubleclick.net", "expires": -1},
            {"name": "auth", "domain": ".adnxs.com", "expires": -1},
        ])
        ok, detail = session_status("wuzzuf", ("wuzzuf.net",))
        self.assertFalse(ok, detail)
        self.assertIn("third-party", detail)

    def test_an_expired_auth_cookie_is_not_a_login(self):
        from auto_apply.browser import session_status

        self._write("bayt", [
            {"name": "token", "domain": ".bayt.com",
             "expires": time.time() - 86400},
        ])
        ok, detail = session_status("bayt", ("bayt.com",))
        self.assertFalse(ok, detail)
        self.assertIn("expired", detail)

    def test_an_advertising_cookie_does_not_false_positive_on_uid(self):
        """`uet_nuuid` contains "uid"; a bare "uid" hint would match it."""
        from auto_apply.browser import AUTH_COOKIE_HINTS

        self.assertNotIn("uid", AUTH_COOKIE_HINTS)

    def test_an_unreadable_state_file_is_reported_not_raised(self):
        from auto_apply.browser import session_status

        (self.tmp / "bayt_state.json").write_text("{not json", encoding="utf-8")
        ok, detail = session_status("bayt", ("bayt.com",))
        self.assertFalse(ok)
        self.assertIn("unreadable", detail)

    def test_the_panel_lists_every_watched_board(self):
        rows = monitor.session_health()
        self.assertEqual([r.label for r in rows], list(monitor.WATCHED_BOARDS))
        for row in rows:
            with self.subTest(board=row.label):
                self.assertTrue(row.detail, "a status with no explanation")


class TestChannelHealth(unittest.TestCase):
    def test_a_missing_listener_is_called_out_loudly(self):
        """The failure this dashboard exists to catch: replies going nowhere."""
        rows = monitor.channel_health(processes=[])
        telegram = next(r for r in rows if r.label == "Telegram listener")
        self.assertFalse(telegram.ok)
        self.assertIn("NOT RUNNING", telegram.detail)

    def test_a_running_listener_is_recognised(self):
        rows = monitor.channel_health(
            processes=["C:\\Python314\\python.exe main.py --listen"]
        )
        telegram = next(r for r in rows if r.label == "Telegram listener")
        self.assertTrue(telegram.ok)

    def test_every_channel_is_reported_on(self):
        labels = {r.label for r in monitor.channel_health(processes=[])}
        for expected in ("Telegram listener", "WhatsApp inbound",
                         "WhatsApp outbound", "Gmail OAuth2 inbox"):
            with self.subTest(channel=expected):
                self.assertIn(expected, labels)

    def test_hunter_mode_reads_the_interval_from_the_command_line(self):
        mode, running = monitor.hunter_mode(
            processes=["python main.py --daemon --interval 60"]
        )
        self.assertTrue(running)
        self.assertIn("60", mode)

    def test_hunter_mode_says_so_when_nothing_is_running(self):
        mode, running = monitor.hunter_mode(processes=[])
        self.assertFalse(running)
        self.assertIn("Not running", mode)

    def test_live_mode_is_recognised_too(self):
        mode, running = monitor.hunter_mode(
            processes=["python main.py --live"])
        self.assertTrue(running)


# ---------------------------------------------------------------------------
class TestLogTail(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.log = self.tmp / "listener.log"

    def test_only_new_lines_are_returned(self):
        self.log.write_text("one\ntwo\n", encoding="utf-8")
        tail = monitor.LogTail({"listener": self.log})
        self.assertEqual([l for _s, l in tail.poll()], ["one", "two"])
        self.assertEqual(tail.poll(), [])
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write("three\n")
        self.assertEqual([l for _s, l in tail.poll()], ["three"])

    def test_arabic_survives_the_tail(self):
        self.log.write_text("تعديل 7 الراتب: 15000 — done\n", encoding="utf-8")
        tail = monitor.LogTail({"listener": self.log})
        [(_source, line)] = tail.poll()
        self.assertIn("الراتب", line)
        self.assertIn("—", line)

    def test_a_utf8_bom_is_not_rendered_as_a_stray_glyph(self):
        """PowerShell's `Add-Content -Encoding utf8` writes one."""
        self.log.write_bytes("\ufeff2026-08-19 supervisor starting\n"
                             .encode("utf-8"))
        tail = monitor.LogTail({"listener": self.log})
        [(_source, line)] = tail.poll()
        self.assertTrue(line.startswith("2026"), repr(line))
        self.assertNotIn("\ufeff", line)

    def test_a_rotated_log_restarts_instead_of_reading_mid_line(self):
        """The supervisor retires a UTF-16 log; keeping the old offset would
        then read from the middle of a line forever."""
        self.log.write_text("old line one\nold line two\n", encoding="utf-8")
        tail = monitor.LogTail({"listener": self.log})
        tail.poll()
        self.log.write_text("fresh\n", encoding="utf-8")     # smaller now
        self.assertEqual([l for _s, l in tail.poll()], ["fresh"])

    def test_a_missing_log_is_survivable(self):
        tail = monitor.LogTail({"listener": self.tmp / "nope.log"})
        tail.prime()
        self.assertEqual(tail.poll(), [])

    def test_undecodable_bytes_do_not_crash_the_tail(self):
        self.log.write_bytes(b"good line\n\xff\xfe\x00bad\n")
        tail = monitor.LogTail({"listener": self.log})
        self.assertTrue(tail.poll())

    def test_the_buffer_is_bounded(self):
        self.log.write_text("\n".join(str(i) for i in range(500)) + "\n",
                            encoding="utf-8")
        tail = monitor.LogTail({"listener": self.log}, keep=50)
        tail.poll()
        self.assertEqual(len(tail.lines), 50)

    def test_priming_starts_near_the_end(self):
        self.log.write_text("x" * 20000 + "\nlast\n", encoding="utf-8")
        tail = monitor.LogTail({"listener": self.log})
        tail.prime(tail_bytes=100)
        self.assertLess(len(tail.lines), 3)


class TestLineStyling(unittest.TestCase):
    def test_outcomes_are_coloured_by_meaning(self):
        for line, expected in (
            ("APPLICATION SUCCESSFULLY SUBMITTED", "good"),
            ("ERROR could not submit", "bad"),
            ("Refusing to submit: search widget", "bad"),
            ("WARNING quota exhausted", "warn"),
            ("APPLICATION DRAFT READY FOR REVIEW", "accent"),
            ("موافق 3", "accent"),
            ("just some text", "dim"),
        ):
            with self.subTest(line=line):
                self.assertEqual(monitor.style_for(line), expected)


class TestRendering(DashboardHarness):
    def _screen(self, colour=False) -> str:
        return monitor.render_plain(
            monitor.collect_kpis(), monitor.session_health(),
            monitor.channel_health(processes=[]),
            monitor.hunter_mode(processes=[]),
            [("listener", "17:27:23 attached as KRIZA_7")],
            colour=colour,
        )

    def test_the_screen_contains_every_required_section(self):
        screen = self._screen()
        for heading in ("MISSION CONTROL", "MODE", "SESSIONS", "CHANNELS",
                        "METRICS", "WAITING ON YOU", "LIVE ACTIVITY"):
            with self.subTest(heading=heading):
                self.assertIn(heading, screen)

    def test_the_kpis_appear_on_screen(self):
        screen = self._screen()
        self.assertIn("Scanned", screen)
        self.assertIn("Matches", screen)
        self.assertIn("Submitted", screen)

    def test_pending_drafts_are_named_with_their_reply_syntax(self):
        screen = self._screen()
        self.assertIn("[DRAFT #1]", screen)
        self.assertIn("done <id>", screen)

    def test_arabic_renders_without_mojibake(self):
        screen = self._screen()
        self.assertIn("شركة صحرا", screen)
        self.assertNotIn("ÙØ±ÙØ©", screen)

    def test_no_colour_means_no_escape_codes(self):
        self.assertNotIn("\033", self._screen(colour=False))

    def test_colour_mode_emits_escape_codes(self):
        self.assertIn("\033", self._screen(colour=True))

    def test_rendering_never_raises_on_an_empty_system(self):
        monitor.JOBS_DB = self.tmp / "absent.db"
        monitor.VAULT_DB = self.tmp / "absent2.db"
        screen = monitor.render_plain(
            monitor.collect_kpis(), [], [], ("Not running", False), [],
            colour=False,
        )
        self.assertIn("MISSION CONTROL", screen)
        self.assertIn("no log output yet", screen)


class TestEntryPoint(DashboardHarness):
    def test_once_prints_a_snapshot_and_exits_zero(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = monitor.main(["--once", "--no-colour"])
        self.assertEqual(code, 0)
        self.assertIn("MISSION CONTROL", buffer.getvalue())

    def test_the_module_does_nothing_at_import(self):
        """Importing it must not clear the screen or start a loop."""
        again = _load_monitor()
        self.assertTrue(hasattr(again, "main"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
