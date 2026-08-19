"""
The anti-spam shield: proof that running the tests cannot message the user.

This file exists because the suite once did exactly that. Every module set
DRY_RUN=true, all 220 assertions passed, and five real job cards landed in the
user's Telegram Saved Messages on every run -- because `send_via_telegram` had
no dry-run guard and `dispatch()` called it in BOTH arms of its `if dry_run`.
A green suite proved nothing about delivery, so these tests assert on delivery
directly.

Two distinct things are checked, and they must stay distinct:

  * THE PRODUCTION CODE honours DRY_RUN. Asserted against the REAL methods,
    pulled out of conftest's stash -- testing the stub would prove only that
    the stub works.
  * THE HARNESS is armed. Asserted against the live process: sockets blocked,
    transports replaced, credentials neutered.

Run:  python -m pytest tests/test_no_real_sends.py -v
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import OUTBOX, REAL, NetworkBlockedInTests    # noqa: E402
from db import Database                                     # noqa: E402
from models import Evaluation                               # noqa: E402
from notifier import WhatsAppNotifier                       # noqa: E402

# `conftest.py` is a pytest concept. `python -m unittest discover -s tests`
# imports these modules but never calls `pytest_configure`, so the transport
# stubs are never installed and the suite runs against live Telegram. Fail here,
# immediately and legibly, rather than letting 270 tests run unguarded.
if not REAL:
    raise RuntimeError(
        "The test-isolation harness is not active.\n"
        "These tests must be run with pytest, which loads tests/conftest.py:\n"
        "    python -m pytest\n"
        "Running them under `unittest discover` skips the transport stubs and "
        "the socket block, and the suite will message your real accounts."
    )


def _eval(fingerprint: str = "fp-nosend") -> Evaluation:
    return Evaluation(
        fingerprint=fingerprint, company_name="Etisalat",
        role_title="VoIP Engineer", location="Dubai", match_score=91,
        source_platform="linkedin", direct_link="https://example.com/job",
        why_matched="SIP and Asterisk overlap.",
    )


class TestProductionCodeHonoursDryRun(unittest.TestCase):
    """The real methods, not the stubs. This is the bug that shipped."""

    def setUp(self):
        self.n = WhatsAppNotifier(None)
        self.n.dry_run = True

    def test_real_send_via_telegram_returns_without_delivering(self):
        real = REAL["send_via_telegram"]
        ok, detail = real(self.n, "🚨 NEW HIGH-MATCH JOB #101")
        self.assertTrue(ok, "a dry run should report success, not failure")
        self.assertEqual(
            detail, "dry_run",
            "send_via_telegram must short-circuit on dry_run BEFORE it builds "
            "a Telethon client -- this is the guard whose absence spammed the "
            "user's Saved Messages on every test run",
        )

    def test_real_send_via_telegram_does_not_build_a_client(self):
        """The guard must come before the import, not after it."""
        import scrapers.telegram_user_client as tuc

        original = tuc.build_client
        calls = []

        def tripwire(*args, **kwargs):
            calls.append(1)
            raise AssertionError("a Telegram client was built during a dry run")

        tuc.build_client = tripwire
        try:
            REAL["send_via_telegram"](self.n, "hello")
        finally:
            tuc.build_client = original
        self.assertEqual(calls, [], "dry_run reached the transport layer")

    def test_real_send_raw_delivers_on_neither_channel(self):
        ok, detail = REAL["send_raw"](self.n, "digest part 1/2")
        self.assertTrue(ok)
        self.assertEqual(detail, "dry_run")

    def test_dispatch_sends_nothing_and_banks_nothing(self):
        db = Database(Path(tempfile.mkdtemp()) / "nosend.db")
        try:
            n = WhatsAppNotifier(db)
            n.dry_run = True
            OUTBOX.clear()
            result = n.dispatch([_eval("fp-dispatch-dry")])

            self.assertEqual(result.sent, 1, "the dry run should still report")
            self.assertFalse(
                db.already_alerted("fp-dispatch-dry"),
                "a dry run banked as 'sent' would suppress the real alert",
            )
            self.assertEqual(
                [r["status"] for r in db._conn.execute(
                    "SELECT status FROM alerts WHERE fingerprint='fp-dispatch-dry'"
                )],
                ["dry_run", "dry_run"],
                "both channels must be recorded as dry_run, never as sent",
            )
        finally:
            db.close()

    def test_dispatch_dry_run_has_one_telegram_branch_not_two(self):
        """Regression: the `if dry_run` / `elif available` arms were identical.

        Both called send_via_telegram, so dry_run changed nothing about
        Telegram delivery. Asserting on the source keeps the two arms from
        drifting back apart.
        """
        import inspect

        src = inspect.getsource(WhatsAppNotifier.dispatch)
        self.assertNotIn(
            "elif self._telegram_available()", src,
            "dispatch() has a second Telegram arm again; dry_run will leak",
        )
        self.assertIn("if self.dry_run or self._telegram_available()", src)


class TestHarnessIsArmed(unittest.TestCase):
    """conftest.py is doing its job in this very process."""

    def test_outbound_sockets_are_blocked(self):
        with self.assertRaises(NetworkBlockedInTests):
            socket.create_connection(("api.telegram.org", 443), timeout=1)
        # `closing` matters: an unclosed socket surfaces as an unraisable
        # exception at finalization, which -W error turns into a failure.
        with socket.socket() as sock:
            with self.assertRaises(NetworkBlockedInTests):
                sock.connect(("api.callmebot.com", 443))
        with socket.socket() as sock:
            with self.assertRaises(NetworkBlockedInTests):
                sock.connect_ex(("generativelanguage.googleapis.com", 443))

    def test_loopback_stays_open(self):
        """asyncio's self-pipe is a localhost socketpair; blocking it breaks
        unrelated tests for no security benefit."""
        a, b = socket.socketpair()
        a.close()
        b.close()

    def test_transports_are_replaced_with_recorders(self):
        n = WhatsAppNotifier(None)
        n.dry_run = False          # even with dry_run OFF, nothing may leave
        OUTBOX.clear()
        ok, detail = n.send_via_telegram("would-be-spam")
        self.assertTrue(ok)
        self.assertEqual(detail, "blocked-in-tests")
        self.assertEqual(OUTBOX.telegram, ["would-be-spam"])

    def test_http_transport_raises_rather_than_reaching_the_wire(self):
        import http_client

        with self.assertRaises(NetworkBlockedInTests):
            http_client.get("https://api.callmebot.com/whatsapp.php?text=hi")

    def test_real_credentials_are_not_visible_to_tests(self):
        """A stray call must authenticate as nobody, not as the user."""
        self.assertEqual(os.environ.get("TELEGRAM_STRING_SESSION"), "")
        self.assertEqual(os.environ.get("TELEGRAM_API_ID"), "")
        self.assertEqual(os.environ.get("JOB_EMAIL_APP_PASSWORD"), "")
        self.assertNotIn("AIza", os.environ.get("GEMINI_API_KEY", ""),
                         "the real Gemini key is reachable from the test suite")

    def test_the_vault_key_is_a_throwaway(self):
        """Tests must not read, and cannot corrupt, secrets/vault.key."""
        from vault import KEY_PATH

        env_key = os.environ.get("VAULT_KEY", "")
        self.assertTrue(env_key, "no throwaway vault key was generated")
        if KEY_PATH.exists():
            self.assertNotEqual(
                env_key, KEY_PATH.read_text(encoding="utf-8").strip(),
                "the test suite is using the real vault key",
            )


class TestEveryTelegramCallSiteIsGuarded(unittest.TestCase):
    """One guard protects six call sites -- so it must stay the only route."""

    def test_no_module_bypasses_send_via_telegram(self):
        """Nothing may call Telethon's send_message except the notifier."""
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if parts & {"tests", ".venv", "venv", "__pycache__"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "send_message(" in text and path.name not in (
                "notifier.py", "telegram_user_client.py", "auth_telegram.py",
                "check_telegram.py", "setup_wizard.py",
            ):
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(
            offenders, [],
            "these modules reach Telegram without going through "
            "WhatsAppNotifier.send_via_telegram, so they ignore DRY_RUN",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
