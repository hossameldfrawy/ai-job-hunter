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

import contextlib
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


@contextlib.contextmanager
def _real_callmebot():
    """Put the genuine `_send_callmebot` back for the duration of a test.

    The suite replaces it with a recorder so the WhatsApp half of every
    dual-channel card can be asserted on. Anything proving that the PRODUCTION
    code honours DRY_RUN has to reach past that recorder, or it proves only
    that the recorder works -- which is the exact class of mistake this file
    exists to catch.
    """
    stub = WhatsAppNotifier._send_callmebot
    WhatsAppNotifier._send_callmebot = REAL["_send_callmebot"]
    try:
        yield
    finally:
        WhatsAppNotifier._send_callmebot = stub


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

    def test_real_send_photo_returns_without_uploading(self):
        """The evidence screenshot rides the same guard as the text card."""
        ok, detail = REAL["send_photo_via_telegram"](
            self.n, "screenshots/20260819-101500_app7.png", "[DRAFT #7]"
        )
        self.assertTrue(ok, "a dry run should report success, not failure")
        self.assertEqual(
            detail, "dry_run",
            "send_photo_via_telegram must short-circuit on dry_run BEFORE it "
            "builds a Telethon client -- it is a second route to the same "
            "Saved Messages inbox, so it needs the same guard",
        )

    def test_real_send_callmebot_delivers_nothing_on_a_dry_run(self):
        """The WhatsApp leg, proven against the real method.

        `_send_callmebot` is stubbed for the rest of the suite so tests can
        read the WhatsApp card. That makes THIS assertion the only place the
        production guard is actually exercised, so it drives the stashed
        original and watches the transport underneath it.
        """
        import http_client

        attempts = []

        def tripwire(url, *args, **kwargs):
            attempts.append(url)
            raise AssertionError("CallMeBot was called during a dry run")

        original = http_client.get
        http_client.get = tripwire
        try:
            ok, detail = REAL["_send_callmebot"](self.n, "🚨 job card")
        finally:
            http_client.get = original
        self.assertTrue(ok)
        self.assertEqual(detail, "dry_run")
        self.assertEqual(attempts, [], "dry_run reached the HTTP transport")

    def test_the_telegram_senders_work_from_inside_a_running_event_loop(self):
        """Regression, found in production.

        `asyncio.run()` RAISES when a loop is already running in this thread,
        and the coroutine it was given is then never awaited. The poll-mode
        review listener calls the notifier from inside its own loop, so every
        Telegram reply failed with "telegram fallback failed: RuntimeError"
        while WhatsApp went out perfectly -- the user saw half a conversation
        and the log line said "delivered".
        """
        import asyncio
        import warnings

        async def from_inside_a_loop():
            return (
                REAL["send_via_telegram"](self.n, "reply from the poll loop"),
                REAL["send_photo_via_telegram"](self.n, "/x/shot.png", "cap"),
            )

        with warnings.catch_warnings():
            # An un-awaited coroutine surfaces as a RuntimeWarning; -W error
            # would turn the old bug into a failure here rather than a silent
            # wrong answer, so make sure it cannot be filtered away.
            warnings.simplefilter("error", RuntimeWarning)
            (text_ok, text_detail), (photo_ok, photo_detail) = asyncio.run(
                from_inside_a_loop()
            )

        self.assertTrue(text_ok)
        self.assertEqual(text_detail, "dry_run")
        self.assertTrue(photo_ok)
        self.assertEqual(photo_detail, "dry_run")

    def test_the_loop_safe_runner_returns_the_coroutines_value(self):
        import asyncio

        from notifier import _run_coroutine

        async def answer():
            return 42

        self.assertEqual(_run_coroutine(answer), 42)

        async def outer():
            return _run_coroutine(answer)

        self.assertEqual(asyncio.run(outer()), 42,
                         "the value was lost when a loop was already running")

    def test_the_loop_safe_runner_propagates_failures(self):
        import asyncio

        from notifier import _run_coroutine

        async def explode():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            _run_coroutine(explode)

        async def outer():
            return _run_coroutine(explode)

        with self.assertRaises(ValueError):
            asyncio.run(outer())

    def test_real_send_raw_delivers_on_neither_channel(self):
        """CallMeBot first, Telegram fallback -- neither may deliver.

        Both legs are restored from the stash: `send_raw` calls
        `_send_callmebot` through `self`, so leaving the recorder in place
        would prove only that the recorder returns success.
        """
        with _real_callmebot():
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

    def test_a_real_browser_cannot_be_launched(self):
        """A Chromium is not a socket, so the network block never saw it.

        A harness that stubbed `browser_page` but not `browser_context` drove a
        live job board for two seconds on every run and passed while doing it.
        """
        from auto_apply.browser import playwright_available

        if not playwright_available():
            self.skipTest("Playwright is not installed on this machine")
        import playwright.sync_api as pw

        with self.assertRaises(NetworkBlockedInTests):
            pw.sync_playwright()

    def test_the_apply_flow_cannot_reach_a_real_browser(self):
        """Both entry points, since stubbing one and not the other is the bug."""
        from auto_apply.browser import playwright_available

        if not playwright_available():
            self.skipTest("Playwright is not installed on this machine")
        import auto_apply.browser as browser_mod

        for opener in (browser_mod.browser_context, browser_mod.browser_page):
            with self.subTest(opener=opener.__name__):
                with self.assertRaises(NetworkBlockedInTests):
                    with opener("tanqeeb"):
                        pass

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
    """One guard protects every call site -- so it must stay the only route."""

    #: Modules that legitimately hold a Telethon client. Everything else has to
    #: go through the notifier, which is where the DRY_RUN guard lives.
    ALLOWED = (
        "notifier.py", "telegram_user_client.py", "auth_telegram.py",
        "check_telegram.py", "setup_wizard.py",
    )

    def _offenders(self, needle: str) -> list[str]:
        root = Path(__file__).resolve().parent.parent
        found = []
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if parts & {"tests", ".venv", "venv", "__pycache__"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if needle in text and path.name not in self.ALLOWED:
                found.append(str(path.relative_to(root)))
        return found

    def test_no_module_bypasses_send_via_telegram(self):
        """Nothing may call Telethon's send_message except the notifier."""
        self.assertEqual(
            self._offenders("send_message("), [],
            "these modules reach Telegram without going through "
            "WhatsAppNotifier.send_via_telegram, so they ignore DRY_RUN",
        )

    def test_no_module_uploads_a_file_to_telegram_directly(self):
        """The evidence screenshot is a SECOND route into the same inbox.

        `send_file` bypasses `send_message` entirely, so the original scan
        would not have caught a module pushing screenshots to the user's Saved
        Messages on every test run.
        """
        self.assertEqual(
            self._offenders("send_file("), [],
            "these modules upload to Telegram without going through "
            "WhatsAppNotifier.send_photo_via_telegram, so they ignore DRY_RUN",
        )


class TestTheReviewFlowCannotReachTheUser(unittest.TestCase):
    """The HITL engine is the newest, chattiest source of outbound traffic.

    It emits a card when a draft is prepared, another after every edit, a
    confirmation with a screenshot on submission, and a reply to `status` and
    `help`. All of it lands in the same Saved Messages inbox the suite once
    spammed, so it gets the same proof.
    """

    def setUp(self):
        from vault import SecureStore

        self.store = SecureStore(Path(tempfile.mkdtemp()) / "review.db")
        self.notifier = WhatsAppNotifier(None)
        self.notifier.dry_run = True
        OUTBOX.clear()

    def tearDown(self):
        self.store.close()

    def _draft(self):
        return self.store.record_application(
            job_fingerprint="fp-nosend", job_id=101, company="Etisalat",
            role="VoIP Engineer", platform="tanqeeb:uae",
            job_url="https://uae.tanqeeb.com/1.html",
            payload={"fields": {}, "field_map": [], "draft": {},
                     "form_ok": True},
            cover_letter="A letter", status="review_pending",
        )

    def test_a_review_card_is_recorded_on_both_channels_and_delivered_on_none(self):
        from auto_apply.review import DraftCard, dispatch_review

        dispatch_review(self.notifier,
                        DraftCard.from_row(self.store.get_application(self._draft())))
        self.assertEqual(len(OUTBOX.telegram), 1)
        self.assertEqual(len(OUTBOX.whatsapp), 1)
        self.assertEqual(OUTBOX.http, [], "an outbound HTTP call was attempted")

    def test_send_dual_honours_dry_run_on_the_real_transports(self):
        """Both legs of the real method, with the recorders lifted."""
        import http_client

        attempts = []
        original_get = http_client.get
        http_client.get = lambda url, *a, **k: attempts.append(url)
        stub_tg = WhatsAppNotifier.send_via_telegram
        stub_wa = WhatsAppNotifier._send_callmebot
        stub_photo = WhatsAppNotifier.send_photo_via_telegram
        WhatsAppNotifier.send_via_telegram = REAL["send_via_telegram"]
        WhatsAppNotifier._send_callmebot = REAL["_send_callmebot"]
        WhatsAppNotifier.send_photo_via_telegram = REAL["send_photo_via_telegram"]
        try:
            result = self.notifier.send_dual("full card", "short card",
                                             photo="/nonexistent/shot.png")
        finally:
            http_client.get = original_get
            WhatsAppNotifier.send_via_telegram = stub_tg
            WhatsAppNotifier._send_callmebot = stub_wa
            WhatsAppNotifier.send_photo_via_telegram = stub_photo

        self.assertTrue(result.telegram_ok)
        self.assertTrue(result.whatsapp_ok)
        self.assertEqual(result.telegram_detail, "dry_run")
        self.assertEqual(result.whatsapp_detail, "dry_run")
        self.assertEqual(result.photo_detail, "dry_run")
        self.assertEqual(attempts, [], "dry_run reached CallMeBot")

    def test_the_controller_answers_through_the_notifier_only(self):
        from auto_apply.control import ReviewController

        controller = ReviewController(self.store, self.notifier,
                                      submit_fn=lambda *a, **k: True,
                                      approve_fn=lambda app_id, store: True)
        self._draft()
        controller.handle("status")
        controller.handle("help")
        self.assertTrue(OUTBOX.telegram, "the reply never reached the recorder")
        self.assertEqual(OUTBOX.http, [])

    def test_the_listener_ignores_its_own_cards_in_this_very_process(self):
        """Belt and braces: the loop that could message the user is the same
        loop that reads her Saved Messages."""
        from auto_apply.control import ReviewController, TelegramCommandListener
        from auto_apply.review import BOT_MARK

        listener = TelegramCommandListener(
            ReviewController(self.store, self.notifier,
                             submit_fn=lambda *a, **k: True),
            owner_id=None, max_age_minutes=0,
        )
        app_id = self._draft()
        self.assertIsNone(listener.handle_message(
            f"{BOT_MARK}done {app_id}"
        ))
        self.assertEqual(
            self.store.get_application(app_id)["status"], "review_pending",
            "the bot submitted an application by reading its own card",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
