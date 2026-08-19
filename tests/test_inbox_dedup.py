"""
Inbox monitor: exactly-once alerting, and not one wasted Gemini call.

The monitor re-reads the same mailbox every 15 minutes, so every property that
matters here is about what happens on the SECOND pass over a message it has
already seen. Two distinct leaks used to live in that gap:

  * A message the local keyword gate let through but Gemini judged NOT job
    mail was never recorded and never flagged read, so the next poll paid for
    the identical classification again. One job-alert digest -- which says
    "position" and so clears the gate every time -- cost ~96 Gemini calls a
    day for as long as it sat unread.

  * `alerted` was written from `should_alert` BEFORE the send, so a delivery
    that failed -- or a monitor built with no notifier at all -- was banked as
    "the user has been told".

Run:  python -m pytest tests/test_inbox_dedup.py -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_apply.email_listener import EmailMonitor, InboxMessage, MailBackend  # noqa: E402
from vault import SecureStore                                                  # noqa: E402


def _store() -> SecureStore:
    return SecureStore(Path(tempfile.mkdtemp()) / "vault.db")


def _msg(uid: str, subject: str, body: str = "", sender: str = "hr@etisalat.ae",
         message_id: str | None = None) -> InboxMessage:
    return InboxMessage(
        uid=uid, message_id=message_id or f"<{uid}@mail.example>",
        sender=sender, subject=subject, body=body or subject,
        received=datetime.now(timezone.utc),
    )


class FakeBackend(MailBackend):
    """A mailbox that keeps handing back the same unread mail, like a real one."""

    name = "fake"

    def __init__(self, messages: list[InboxMessage]):
        self.messages = messages
        self.marked: list[str] = []

    def fetch_unread(self, lookback_days: int, limit: int) -> list[InboxMessage]:
        # Deliberately NOT filtered by self.marked: some mail stays unread
        # between polls (mark_seen_when_classified off, a failed modify, the
        # user marking it unread again), and the dedup must not depend on it.
        return list(self.messages)

    def mark_seen(self, message: InboxMessage) -> None:
        self.marked.append(message.uid)


class RecordingNotifier:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: list[str] = []

    def send_via_telegram(self, message: str):
        self.sent.append(message)
        return (True, "sent") if self.ok else (False, "telegram down")


class CountingMonitor(EmailMonitor):
    """An EmailMonitor whose Gemini calls are counted instead of billed."""

    def __init__(self, *args, verdict=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.classify_calls = 0
        self._verdict = verdict or {}

    def classify(self, message):
        self.classify_calls += 1
        return dict(self._verdict)


_INTERVIEW = {
    "is_job_related": True, "classification": "interview",
    "company": "Etisalat", "role": "VoIP Engineer",
    "meeting_datetime": "Tuesday 14:00 GST",
    "meeting_link": "https://zoom.us/j/123", "action_required": "Confirm",
    "summary": "Interview invitation.", "urgency": "high",
}
_NOT_JOB = {
    "is_job_related": False, "classification": "other", "company": "",
    "role": "", "meeting_datetime": "", "meeting_link": "",
    "action_required": "", "summary": "Marketing newsletter.", "urgency": "low",
}


class TestGeminiQuotaIsNotLeaked(unittest.TestCase):
    """The expensive call must happen at most once per message, ever."""

    def setUp(self):
        self.v = _store()

    def tearDown(self):
        self.v.close()

    def test_a_not_job_verdict_is_banked_and_never_re_billed(self):
        # A job-board digest: it passes the keyword gate ("position") but is
        # not an application update, so Gemini is the only thing that can
        # tell them apart -- and it must only have to do so once.
        backend = FakeBackend([_msg("1", "10 new positions matching your search")])
        monitor = CountingMonitor(self.v, notifier=None, backend=backend,
                                  verdict=_NOT_JOB)

        for _ in range(5):                      # five polls over the same inbox
            monitor.run_once()

        self.assertEqual(
            monitor.classify_calls, 1,
            "the same message was sent to Gemini once per poll; a newsletter "
            "sitting unread would bill the quota forever",
        )

    def test_a_not_job_verdict_is_never_marked_read(self):
        """Only genuine job mail may be touched in a personal inbox."""
        backend = FakeBackend([_msg("1", "New positions this week")])
        monitor = CountingMonitor(self.v, notifier=None, backend=backend,
                                  verdict=_NOT_JOB)
        monitor.run_once()
        self.assertEqual(backend.marked, [])

    def test_the_local_gate_keeps_obvious_non_job_mail_away_from_gemini(self):
        backend = FakeBackend([
            _msg("1", "Your electricity bill is ready"),
            _msg("2", "Weekend plans?"),
            _msg("3", "Photos from the trip"),
        ])
        monitor = CountingMonitor(self.v, notifier=None, backend=backend,
                                  verdict=_NOT_JOB)
        counts = monitor.run_once()
        self.assertEqual(monitor.classify_calls, 0,
                         "mail with no job keyword must never reach Gemini")
        self.assertEqual(counts["skipped"], 3)

    def test_job_mail_is_classified_once_across_repeated_polls(self):
        backend = FakeBackend([_msg("1", "Interview invitation - VoIP Engineer")])
        monitor = CountingMonitor(self.v, notifier=RecordingNotifier(),
                                  backend=backend, verdict=_INTERVIEW)
        for _ in range(4):
            monitor.run_once()
        self.assertEqual(monitor.classify_calls, 1)


class TestExactlyOnceAlerting(unittest.TestCase):
    """No duplicate Telegram message for the same email. Ever."""

    def setUp(self):
        self.v = _store()

    def tearDown(self):
        self.v.close()

    def test_one_alert_across_many_polls(self):
        notifier = RecordingNotifier()
        backend = FakeBackend([_msg("1", "Interview invitation - VoIP Engineer")])
        monitor = CountingMonitor(self.v, notifier=notifier, backend=backend,
                                  verdict=_INTERVIEW)
        for _ in range(6):
            monitor.run_once()
        self.assertEqual(len(notifier.sent), 1,
                         "the user received the same invitation six times")

    def test_the_same_message_twice_in_one_fetch_alerts_once(self):
        """A lapped or overlapping poll can hand back a duplicate."""
        duplicate = _msg("1", "Interview invitation")
        notifier = RecordingNotifier()
        backend = FakeBackend([duplicate, duplicate])
        monitor = CountingMonitor(self.v, notifier=notifier, backend=backend,
                                  verdict=_INTERVIEW)
        monitor.run_once()
        self.assertEqual(len(notifier.sent), 1)

    def test_two_messages_sharing_a_message_id_alert_once(self):
        """message_id is the dedup key, not the mailbox uid."""
        notifier = RecordingNotifier()
        backend = FakeBackend([
            _msg("1", "Interview invitation", message_id="<same@corp>"),
            _msg("2", "Interview invitation (resent)", message_id="<same@corp>"),
        ])
        monitor = CountingMonitor(self.v, notifier=notifier, backend=backend,
                                  verdict=_INTERVIEW)
        monitor.run_once()
        self.assertEqual(len(notifier.sent), 1)

    def test_a_failed_send_is_recorded_as_not_alerted(self):
        notifier = RecordingNotifier(ok=False)
        backend = FakeBackend([_msg("1", "Interview invitation")])
        monitor = CountingMonitor(self.v, notifier=notifier, backend=backend,
                                  verdict=_INTERVIEW)
        counts = monitor.run_once()

        self.assertEqual(counts["alerted"], 0)
        event = self.v.recent_events(1)[0]
        self.assertEqual(
            event["alerted"], 0,
            "a failed delivery was banked as 'the user has been told'",
        )

    def test_a_failed_send_is_not_retried_into_a_duplicate(self):
        """Recording before the send is what buys exactly-once."""
        notifier = RecordingNotifier(ok=False)
        backend = FakeBackend([_msg("1", "Interview invitation")])
        monitor = CountingMonitor(self.v, notifier=notifier, backend=backend,
                                  verdict=_INTERVIEW)
        for _ in range(3):
            monitor.run_once()
        self.assertEqual(len(notifier.sent), 1)

    def test_no_notifier_does_not_claim_the_user_was_alerted(self):
        backend = FakeBackend([_msg("1", "Interview invitation")])
        monitor = CountingMonitor(self.v, notifier=None, backend=backend,
                                  verdict=_INTERVIEW)
        monitor.run_once()
        self.assertEqual(self.v.recent_events(1)[0]["alerted"], 0)

    def test_a_notifier_returning_a_bare_bool_does_not_crash_the_loop(self):
        class LegacyNotifier:
            def __init__(self):
                self.sent = []

            def send_via_telegram(self, message):
                self.sent.append(message)
                return True

        notifier = LegacyNotifier()
        backend = FakeBackend([_msg("1", "Interview invitation")])
        monitor = CountingMonitor(self.v, notifier=notifier, backend=backend,
                                  verdict=_INTERVIEW)
        counts = monitor.run_once()
        self.assertEqual(counts["alerted"], 1)
        self.assertEqual(self.v.recent_events(1)[0]["alerted"], 1)


class TestClassificationRouting(unittest.TestCase):
    """Which verdicts interrupt the user, and which are only recorded."""

    def setUp(self):
        self.v = _store()

    def tearDown(self):
        self.v.close()

    def _run(self, kind: str) -> RecordingNotifier:
        verdict = dict(_INTERVIEW, classification=kind)
        notifier = RecordingNotifier()
        backend = FakeBackend([_msg(kind, f"Re: your application ({kind})")])
        CountingMonitor(self.v, notifier=notifier, backend=backend,
                        verdict=verdict).run_once()
        return notifier

    def test_interviews_and_assessments_alert(self):
        for kind in ("interview", "assessment", "recruiter_outreach"):
            with self.subTest(kind=kind):
                self.assertEqual(len(self._run(kind).sent), 1)

    def test_rejections_and_acknowledgments_are_recorded_silently(self):
        for kind in ("rejection", "acknowledgment", "other"):
            with self.subTest(kind=kind):
                notifier = self._run(kind)
                self.assertEqual(
                    notifier.sent, [],
                    f"a {kind} interrupted the user; it belongs in the log",
                )
        self.assertEqual(len(self.v.recent_events(10)), 3,
                         "silent does not mean unrecorded")


class TestEmptyAndBrokenMailbox(unittest.TestCase):
    """Zero unread is the normal case, not an edge case."""

    def setUp(self):
        self.v = _store()

    def tearDown(self):
        self.v.close()

    def test_zero_unread_messages_is_a_clean_pass(self):
        monitor = CountingMonitor(self.v, notifier=RecordingNotifier(),
                                  backend=FakeBackend([]), verdict=_INTERVIEW)
        counts = monitor.run_once()
        self.assertEqual(
            counts, {"scanned": 0, "skipped": 0, "classified": 0, "alerted": 0}
        )
        self.assertEqual(monitor.classify_calls, 0)

    def test_a_classification_failure_does_not_stop_the_other_messages(self):
        class Flaky(CountingMonitor):
            def classify(self, message):
                self.classify_calls += 1
                if message.uid == "1":
                    raise RuntimeError("Gemini 503")
                return dict(_INTERVIEW)

        notifier = RecordingNotifier()
        backend = FakeBackend([_msg("1", "Interview invitation A"),
                               _msg("2", "Interview invitation B")])
        counts = Flaky(self.v, notifier=notifier, backend=backend).run_once()
        self.assertEqual(counts["alerted"], 1)
        self.assertEqual(len(notifier.sent), 1)

    def test_a_transient_failure_is_retried_on_the_next_pass(self):
        """A 503 must not be banked like a verdict; only real answers stick."""
        state = {"fail": True}

        class Flaky(CountingMonitor):
            def classify(self, message):
                self.classify_calls += 1
                if state["fail"]:
                    raise RuntimeError("Gemini 503")
                return dict(_INTERVIEW)

        notifier = RecordingNotifier()
        monitor = Flaky(self.v, notifier=notifier,
                        backend=FakeBackend([_msg("1", "Interview invitation")]))
        monitor.run_once()
        self.assertEqual(len(notifier.sent), 0)

        state["fail"] = False
        monitor.run_once()
        self.assertEqual(len(notifier.sent), 1,
                         "a transient Gemini error permanently lost the alert")

    def test_an_unreadable_mailbox_returns_zeroes_rather_than_raising(self):
        class DeadBackend(MailBackend):
            name = "dead"

            def fetch_unread(self, lookback_days, limit):
                raise RuntimeError("token revoked")

        counts = CountingMonitor(self.v, notifier=None,
                                 backend=DeadBackend()).run_once()
        self.assertEqual(counts["scanned"], 0)
        self.assertEqual(counts["alerted"], 0)


class TestEventLedger(unittest.TestCase):
    """The table the dedup relies on."""

    def setUp(self):
        self.v = _store()

    def tearDown(self):
        self.v.close()

    def test_recording_the_same_message_twice_returns_the_same_row(self):
        first = self.v.record_email_event(
            message_id="<a@x>", sender="hr@x", subject="A",
            classification="interview")
        second = self.v.record_email_event(
            message_id="<a@x>", sender="hr@x", subject="A (again)",
            classification="interview")
        self.assertEqual(first, second, "the conflict path returned a stale id")
        self.assertEqual(len(self.v.recent_events(10)), 1)

    def test_a_conflict_does_not_return_a_previous_messages_id(self):
        """`lastrowid` after DO NOTHING points at the last real insert."""
        first = self.v.record_email_event(
            message_id="<first@x>", sender="a", subject="first",
            classification="interview")
        self.v.record_email_event(
            message_id="<second@x>", sender="b", subject="second",
            classification="rejection")
        again = self.v.record_email_event(
            message_id="<first@x>", sender="a", subject="first",
            classification="interview")
        self.assertEqual(again, first)

    def test_seen_message_is_the_gate(self):
        self.assertFalse(self.v.seen_message("<new@x>"))
        self.v.record_email_event(message_id="<new@x>", sender="", subject="",
                                  classification="not_job_related")
        self.assertTrue(self.v.seen_message("<new@x>"))

    def test_mark_event_alerted_flips_the_flag_both_ways(self):
        self.v.record_email_event(message_id="<m@x>", sender="", subject="",
                                  classification="interview", alerted=False)
        self.v.mark_event_alerted("<m@x>", True)
        self.assertEqual(self.v.recent_events(1)[0]["alerted"], 1)
        self.v.mark_event_alerted("<m@x>", False)
        self.assertEqual(self.v.recent_events(1)[0]["alerted"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
