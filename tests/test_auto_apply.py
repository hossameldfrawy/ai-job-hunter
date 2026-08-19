"""
Phase 2: vault, apply engine, form inspection and inbox triage.

Everything here runs offline: no browser, no IMAP, no Gemini. The parts that
genuinely need a network are exercised by the live checks in the README, not by
the unit suite -- a test that silently emails a real recruiter or submits a real
application would be far worse than no test.

The properties under test are the ones where a bug is expensive rather than
merely annoying:
  * a credential must never be readable from the file that gets published
  * LinkedIn must never be automated, from any entry point
  * an application must never submit without approval
  * a personal email must never be marked read or sent to the AI
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("CALLMEBOT_APIKEY", "test-apikey")
os.environ.setdefault("WHATSAPP_PHONE", "+201234567890")
os.environ.setdefault("CV_TEXT", "VoIP engineer with SIP and Issabel PBX. " * 6)
os.environ.setdefault("APPLY_BASE_PASSWORD", "TestSeed99@")
os.environ.setdefault("DRY_RUN", "true")

from models import Evaluation                                   # noqa: E402
from vault import (                                             # noqa: E402
    STATUS_APPROVED, STATUS_DECLINED, STATUS_REVIEW, STATUS_SUBMITTED,
    SecureStore, decrypt, encrypt,
)


def _store() -> SecureStore:
    return SecureStore(Path(tempfile.mkdtemp()) / "vault.db")


class TestVaultEncryption(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(decrypt(encrypt("TestSeed99@")), "TestSeed99@")

    def test_ciphertext_does_not_contain_the_plaintext(self):
        sealed = encrypt("SuperSecret123")
        self.assertNotIn("SuperSecret123", sealed)

    def test_empty_values_are_passed_through(self):
        self.assertEqual(encrypt(""), "")
        self.assertEqual(decrypt(""), "")

    def test_two_encryptions_differ(self):
        """Fernet is randomised; identical passwords must not look identical."""
        self.assertNotEqual(encrypt("same"), encrypt("same"))


class TestCredentialsVault(unittest.TestCase):
    def setUp(self):
        self.v = _store()

    def tearDown(self):
        self.v.close()

    def test_password_round_trips(self):
        self.v.save_credentials("Tanqeeb", "https://tanqeeb.com",
                                "me@example.com", "S3cret!")
        self.assertEqual(self.v.get_credentials("Tanqeeb")["password"], "S3cret!")

    def test_listing_never_exposes_the_secret(self):
        """`list_platforms` is what a report prints; it must be safe."""
        self.v.save_credentials("Wuzzuf", "u", "me@example.com", "TopSecret")
        row = self.v.list_platforms()[0]
        self.assertNotIn("password", row)
        self.assertNotIn("password_encrypted", row)
        self.assertNotIn("TopSecret", json.dumps(row))

    def test_stored_password_is_not_plaintext_on_disk(self):
        """The whole point of the vault: the file must not leak the password."""
        self.v.save_credentials("Talent", "u", "me@example.com", "PlainTextLeak")
        raw = Path(self.v.path).read_bytes()
        self.assertNotIn(b"PlainTextLeak", raw)

    def test_resaving_updates_rather_than_duplicates(self):
        self.v.save_credentials("Tanqeeb", "u", "me@example.com", "first")
        self.v.save_credentials("Tanqeeb", "u", "me@example.com", "second")
        self.assertEqual(len(self.v.list_platforms()), 1)
        self.assertEqual(self.v.get_credentials("Tanqeeb")["password"], "second")

    def test_unknown_platform_returns_none(self):
        self.assertIsNone(self.v.get_credentials("Nope"))


class TestVaultIsolation(unittest.TestCase):
    """The vault must be a different file from the database that gets published."""

    def test_vault_path_is_not_the_public_database(self):
        from config import settings
        from vault import DEFAULT_VAULT_PATH

        self.assertNotEqual(Path(DEFAULT_VAULT_PATH).name, Path(settings.db_path).name)

    def test_vault_and_key_are_git_ignored(self):
        ignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(
            encoding="utf-8"
        )
        self.assertIn("state/vault.db", ignore)
        self.assertIn("secrets/vault.key", ignore)

    def test_workflow_publishes_only_the_dedup_database(self):
        """Regression guard: the cloud job must never copy the vault."""
        wf = (Path(__file__).resolve().parent.parent
              / ".github" / "workflows" / "job_hunter.yml").read_text(encoding="utf-8")
        self.assertIn("cp state/jobs.db", wf)
        self.assertNotIn("vault.db", wf)


class TestApplicationsHistory(unittest.TestCase):
    def setUp(self):
        self.v = _store()

    def tearDown(self):
        self.v.close()

    def _record(self, fp="fp1", status=STATUS_REVIEW):
        return self.v.record_application(
            job_fingerprint=fp, job_id=101, company="Etisalat",
            role="VoIP Engineer", platform="tanqeeb",
            job_url="https://example.com/job", payload={"a": "b"},
            cover_letter="Dear team", status=status,
        )

    def test_record_and_read_back(self):
        app_id = self._record()
        app = self.v.get_application(app_id)
        self.assertEqual(app["company"], "Etisalat")
        self.assertEqual(app["status"], STATUS_REVIEW)

    def test_fingerprint_is_unique(self):
        first = self._record()
        second = self._record()
        self.assertEqual(first, second, "the same job made two application rows")

    def test_submission_stamps_the_time(self):
        app_id = self._record()
        self.assertIsNone(self.v.get_application(app_id)["submitted_at"])
        self.v.set_application_status(app_id, STATUS_SUBMITTED, screenshot_path="s.png")
        app = self.v.get_application(app_id)
        self.assertIsNotNone(app["submitted_at"])
        self.assertEqual(app["screenshot_path"], "s.png")

    def test_non_submission_does_not_stamp_the_time(self):
        app_id = self._record()
        self.v.set_application_status(app_id, STATUS_DECLINED)
        self.assertIsNone(self.v.get_application(app_id)["submitted_at"])

    def test_company_lookup_ties_an_email_back_to_an_application(self):
        self._record()
        self.assertIsNotNone(self.v.find_application_by_company("Etisalat Group"))
        self.assertIsNone(self.v.find_application_by_company("Nonesuch Ltd"))

    def test_company_lookup_ignores_useless_needles(self):
        self._record()
        self.assertIsNone(self.v.find_application_by_company("ab"))


class TestLinkedInIsNeverAutomated(unittest.TestCase):
    """The single most important rule in Phase 2."""

    def test_linkedin_is_refused(self):
        from auto_apply.engine import is_automatable

        for source in ("linkedin", "LinkedIn", "linkedin:guest", "LINKEDIN"):
            allowed, why = is_automatable(source)
            self.assertFalse(allowed, f"{source} was treated as automatable")
            self.assertIn("manual", why.lower())

    def test_other_boards_are_allowed(self):
        from auto_apply.engine import is_automatable

        for source in ("tanqeeb:egypt", "talent:ae", "api:remoteok"):
            self.assertTrue(is_automatable(source)[0])

    def test_submission_refuses_a_linkedin_application(self):
        from auto_apply.engine import ApplyError, submit_application

        v = _store()
        try:
            app_id = v.record_application(
                job_fingerprint="fp-li", job_id=1, company="X", role="Y",
                platform="linkedin", status=STATUS_APPROVED,
            )
            with self.assertRaises(ApplyError):
                submit_application(app_id, v, notifier=None)
        finally:
            v.close()


class TestApprovalGate(unittest.TestCase):
    """A draft must not be submittable until a human approves it."""

    def setUp(self):
        self.v = _store()
        self.app_id = self.v.record_application(
            job_fingerprint="fp-gate", job_id=1, company="Acme", role="Engineer",
            platform="tanqeeb", job_url="https://example.com/j",
            # form_ok=True because this class tests the APPROVAL gate. The form
            # gate is fail-closed and fires first, so without a confirmed form
            # every case here would be refused for the wrong reason.
            payload={"fields": {"#email": "a@b.c"}, "form_ok": True},
            status=STATUS_REVIEW,
        )

    def tearDown(self):
        self.v.close()

    def test_review_pending_cannot_be_submitted(self):
        from auto_apply.engine import ApplyError, submit_application

        with self.assertRaises(ApplyError) as ctx:
            submit_application(self.app_id, self.v, notifier=None)
        self.assertIn("approve", str(ctx.exception).lower())

    def test_approve_then_submit_is_allowed(self):
        from auto_apply.engine import approve, submit_application

        approve(self.app_id, self.v)
        self.assertEqual(self.v.get_application(self.app_id)["status"],
                         STATUS_APPROVED)
        # dry_run stops before the browser.
        self.assertTrue(submit_application(self.app_id, self.v, None, dry_run=True))

    def test_decline_blocks_submission(self):
        from auto_apply.engine import ApplyError, decline, submit_application

        decline(self.app_id, self.v)
        with self.assertRaises(ApplyError):
            submit_application(self.app_id, self.v, notifier=None)

    def test_unknown_application_is_rejected(self):
        from auto_apply.engine import ApplyError, submit_application

        with self.assertRaises(ApplyError):
            submit_application(999999, self.v, notifier=None)


class TestReviewMessages(unittest.TestCase):
    @staticmethod
    def _ev():
        return Evaluation(
            fingerprint="fp", ref_id=101, company_name="Etisalat",
            role_title="VoIP Engineer", location="Dubai", match_score=88,
            source_platform="tanqeeb:uae", why_matched="Asterisk and SIP overlap.",
        )

    def test_review_message_shows_everything_needed_to_decide(self):
        from auto_apply.engine import format_review_message

        draft = {
            "cover_letter": "I maintain Asterisk and Issabel systems.",
            "answers": [
                {"question": "Years with SIP?", "answer": "Two", "confident": True},
                {"question": "Salary?", "answer": "Unclear", "confident": False},
            ],
            "salary_expectation": "12,000 AED",
        }
        msg = format_review_message(7, self._ev(), draft, "tanqeeb:uae")
        # The reference the user quotes back. It is printed identically on both
        # channels because "done 7" resolves against it, and an id that differs
        # between cards is an id that submits the wrong application.
        self.assertIn("[DRAFT #7]", msg)
        self.assertIn("Etisalat", msg)
        self.assertIn("VoIP Engineer", msg)
        self.assertIn("tanqeeb:uae", msg)
        self.assertIn("88%", msg)
        self.assertIn("Years with SIP?", msg)
        self.assertIn("12,000 AED", msg)
        # Both ways of answering it, because the card is read on a phone far
        # more often than next to a terminal.
        self.assertIn("done 7", msg)
        self.assertIn("موافق 7", msg)
        self.assertIn("--approve 7", msg)
        self.assertIn("--decline 7", msg)

    def test_low_confidence_answers_are_flagged(self):
        from auto_apply.engine import format_review_message

        draft = {"cover_letter": "x", "salary_expectation": "",
                 "answers": [{"question": "Do you have a PMP?",
                              "answer": "No", "confident": False}]}
        self.assertIn("NEEDS YOU",
                      format_review_message(1, self._ev(), draft, "tanqeeb"))

    def test_submitted_message_reports_the_evidence(self):
        from auto_apply.engine import format_submitted_message

        msg = format_submitted_message(9, {
            "company": "Etisalat", "role": "VoIP Engineer",
            "platform": "tanqeeb", "screenshot_path": "/tmp/shot.png",
        })
        self.assertIn("[DRAFT #9]", msg)
        self.assertIn("screenshot saved", msg)
        self.assertIn("shot.png", msg)
        self.assertNotIn("/tmp/", msg,
                         "the card should name the file, not the whole path")

    def test_submitted_message_is_honest_when_no_screenshot(self):
        from auto_apply.engine import format_submitted_message

        self.assertIn("not captured", format_submitted_message(9, {
            "company": "A", "role": "B", "platform": "c", "screenshot_path": "",
        }))


class TestDerivedPasswords(unittest.TestCase):
    def test_each_platform_gets_a_different_password(self):
        from auto_apply.profile_builder import derive_password

        a, b = derive_password("Tanqeeb"), derive_password("Wuzzuf")
        self.assertNotEqual(a, b, "one breach would expose every other board")

    def test_derivation_is_reproducible(self):
        from auto_apply.profile_builder import derive_password

        self.assertEqual(derive_password("Tanqeeb"), derive_password("Tanqeeb"))

    def test_password_meets_common_complexity_rules(self):
        from auto_apply.profile_builder import derive_password

        pw = derive_password("Tanqeeb")
        self.assertGreaterEqual(len(pw), 12)
        self.assertTrue(any(c.isupper() for c in pw))
        self.assertTrue(any(c.islower() for c in pw))
        self.assertTrue(any(c.isdigit() for c in pw))
        self.assertTrue(any(not c.isalnum() for c in pw))

    def test_the_seed_is_not_recoverable_from_the_output(self):
        from auto_apply.profile_builder import derive_password

        self.assertNotIn("TestSeed99@", derive_password("Tanqeeb"))


class TestFormFieldClassification(unittest.TestCase):
    """Boards name inputs arbitrarily; matching is on what a human would read."""

    def test_common_labels_are_recognised(self):
        from auto_apply.browser import _classify

        cases = {
            "Mobile Number": "phone",
            "applicant_email | Your e-mail": "email",
            "Expected Salary (AED)": "salary",
            "Cover Letter": "cover_letter",
            "Upload your CV": "resume",
            "First Name": "first_name",
            "الهاتف": "phone",
            "السيرة الذاتية": "resume",
        }
        for label, expected in cases.items():
            self.assertEqual(_classify(label), expected, f"label={label!r}")

    def test_longest_match_wins(self):
        """'first name' must beat the shorter 'name'."""
        from auto_apply.browser import _classify

        self.assertEqual(_classify("First Name"), "first_name")

    def test_unrecognised_fields_are_marked_unknown_not_guessed(self):
        from auto_apply.browser import _classify

        self.assertEqual(_classify("Describe a difficult outage you resolved"),
                         "unknown")

    def test_unknown_free_text_becomes_a_question_for_the_ai(self):
        from auto_apply.browser import FormField

        f = FormField(selector="#q1", kind="unknown", input_type="textarea",
                      label="Why do you want this role?")
        self.assertTrue(f.is_question)

    def test_a_known_field_is_not_a_question(self):
        from auto_apply.browser import FormField

        f = FormField(selector="#p", kind="phone", input_type="text", label="Phone")
        self.assertFalse(f.is_question)


class TestInboxTriage(unittest.TestCase):
    """A personal email must never reach the AI or be marked read."""

    @staticmethod
    def _msg(subject, body="", sender="a@b.com"):
        from auto_apply.email_listener import InboxMessage

        return InboxMessage(uid="1", message_id="<m1>", sender=sender,
                            subject=subject, body=body, received=None)

    def test_job_mail_passes_the_cheap_filter(self):
        for subject in (
            "Interview invitation - VoIP Engineer",
            "Your application to Etisalat",
            "Technical assessment link",
            "دعوة لإجراء مقابلة",
        ):
            self.assertTrue(self._msg(subject).looks_job_related(), subject)

    def test_personal_mail_is_filtered_out_before_the_ai(self):
        for subject in (
            "Your Amazon order has shipped",
            "Netflix: new password",
            "Family dinner on Friday",
        ):
            self.assertFalse(self._msg(subject).looks_job_related(), subject)

    def test_meeting_links_are_extracted_and_others_ignored(self):
        msg = self._msg(
            "Interview",
            "Join https://zoom.us/j/123456 -- unsubscribe at https://spam.example.com",
        )
        links = msg.candidate_links()
        self.assertIn("https://zoom.us/j/123456", links)
        self.assertNotIn("https://spam.example.com", links)

    def test_assessment_platforms_count_as_meeting_links(self):
        msg = self._msg("Test", "Start at https://www.hackerrank.com/test/abc")
        self.assertTrue(msg.candidate_links())

    def test_interview_alert_carries_time_link_and_application(self):
        from auto_apply.email_listener import EmailMonitor

        result = {
            "classification": "interview", "company": "Etisalat",
            "role": "VoIP Engineer", "meeting_datetime": "Thu 21 Aug, 14:00 GST",
            "meeting_link": "https://zoom.us/j/1", "summary": "Technical round.",
            "action_required": "Confirm attendance",
        }
        app = {"id": 4, "role": "VoIP Engineer", "platform": "tanqeeb",
               "submitted_at": "2026-08-19T10:00:00"}
        msg = EmailMonitor.format_alert(result, app)
        self.assertIn("INTERVIEW INVITATION", msg)
        self.assertIn("Thu 21 Aug", msg)
        self.assertIn("zoom.us", msg)
        self.assertIn("#4", msg)

    def test_alert_is_explicit_when_no_application_matches(self):
        from auto_apply.email_listener import EmailMonitor

        msg = EmailMonitor.format_alert(
            {"classification": "interview", "company": "X", "role": "Y",
             "meeting_datetime": "", "meeting_link": "", "summary": "",
             "action_required": ""},
            None,
        )
        self.assertIn("not linked", msg)

    def test_rejection_does_not_use_the_celebration_wording(self):
        from auto_apply.email_listener import EmailMonitor

        msg = EmailMonitor.format_alert(
            {"classification": "rejection", "company": "X", "role": "Y",
             "meeting_datetime": "", "meeting_link": "", "summary": "",
             "action_required": ""},
            None,
        )
        self.assertNotIn("INTERVIEW INVITATION", msg)


class TestEmailEventStore(unittest.TestCase):
    def setUp(self):
        self.v = _store()

    def tearDown(self):
        self.v.close()

    def test_messages_are_deduplicated(self):
        self.v.record_email_event(message_id="<a>", sender="s", subject="x",
                                  classification="interview")
        self.v.record_email_event(message_id="<a>", sender="s", subject="x",
                                  classification="interview")
        self.assertTrue(self.v.seen_message("<a>"))
        self.assertEqual(len(self.v.recent_events()), 1)

    def test_unseen_message_is_reported_as_such(self):
        self.assertFalse(self.v.seen_message("<never>"))


class TestApplicationFormDetection(unittest.TestCase):
    """Not every form on a job page is the apply form.

    Found live: a Tanqeeb job page yielded the site's SEARCH widget. Filling
    that and clicking submit would have run a search and reported it as a
    submitted application -- worse than failing, because it looks like success.
    """

    @staticmethod
    def _f(kind, selector="#x", label="", input_type="text"):
        from auto_apply.browser import FormField

        return FormField(selector=selector, kind=kind, input_type=input_type,
                         label=label or kind)

    def test_resume_upload_proves_an_application_form(self):
        from auto_apply.browser import looks_like_application_form

        ok, _ = looks_like_application_form(
            [self._f("resume", input_type="file"), self._f("email")]
        )
        self.assertTrue(ok)

    def test_cover_letter_proves_an_application_form(self):
        from auto_apply.browser import looks_like_application_form

        self.assertTrue(looks_like_application_form(
            [self._f("cover_letter", input_type="textarea")]
        )[0])

    def test_two_personal_fields_are_enough(self):
        from auto_apply.browser import looks_like_application_form

        self.assertTrue(looks_like_application_form(
            [self._f("email"), self._f("phone")]
        )[0])

    def test_search_widget_is_rejected(self):
        from auto_apply.browser import looks_like_application_form

        ok, why = looks_like_application_form([
            self._f("unknown", '[name="keywords"]', "Keywords"),
            self._f("location", '[name="state"]', "State"),
        ])
        self.assertFalse(ok)
        self.assertIn("search", why.lower())

    def test_empty_page_is_rejected(self):
        from auto_apply.browser import looks_like_application_form

        self.assertFalse(looks_like_application_form([])[0])

    def test_submission_refuses_when_drafting_found_no_form(self):
        """The guard must fire BEFORE a browser is launched."""
        from auto_apply.engine import ApplyError, submit_application

        v = _store()
        try:
            app_id = v.record_application(
                job_fingerprint="fp-noform", job_id=1, company="Erada",
                role="Help Desk", platform="tanqeeb:egypt",
                job_url="https://example.com/j",
                payload={"fields": {}, "form_ok": False,
                         "form_note": "search widget only"},
                status=STATUS_APPROVED,
            )
            with self.assertRaises(ApplyError) as ctx:
                submit_application(app_id, v, notifier=None, dry_run=False)
            self.assertIn("apply by hand", str(ctx.exception).lower())
        finally:
            v.close()

    def test_submission_proceeds_when_a_real_form_was_found(self):
        from auto_apply.engine import submit_application

        v = _store()
        try:
            app_id = v.record_application(
                job_fingerprint="fp-form", job_id=2, company="Acme",
                role="Engineer", platform="tanqeeb:egypt",
                job_url="https://example.com/j2",
                payload={"fields": {"#email": "a@b.c"}, "form_ok": True},
                status=STATUS_APPROVED,
            )
            self.assertTrue(
                submit_application(app_id, v, notifier=None, dry_run=True)
            )
        finally:
            v.close()

    def test_legacy_records_without_the_flag_are_refused(self):
        """A draft with no recorded verdict must NOT reach a browser.

        This used to assert the opposite -- that an unflagged row "falls
        through to the live check". It does, but the live check runs *inside*
        `browser_page()`, after Chromium has launched and navigated, which is
        precisely what the pre-flight guard exists to prevent.

        Not hypothetical. The pending draft in the real vault had form_ok=None
        and its only detected fields were `keywords` and `state` -- Tanqeeb's
        search box. Under the old guard, `--approve 1` launched a browser at
        it; filling and submitting a search widget produces a "submitted"
        record for an application nobody ever made.
        """
        from auto_apply.engine import ApplyError, submit_application

        v = _store()
        try:
            app_id = v.record_application(
                job_fingerprint="fp-legacy", job_id=3, company="Acme",
                role="Engineer", platform="tanqeeb:egypt",
                job_url="https://example.com/j3",
                payload={"fields": {"#email": "a@b.c"}},   # no form_ok key
                status=STATUS_APPROVED,
            )
            with self.assertRaises(ApplyError) as ctx:
                submit_application(app_id, v, notifier=None, dry_run=True)
            self.assertIn("apply by hand", str(ctx.exception).lower())
            self.assertIn("--apply", str(ctx.exception),
                          "the error should say how to re-inspect the page")
        finally:
            v.close()

    def test_an_empty_payload_is_refused_rather_than_assumed_safe(self):
        from auto_apply.engine import ApplyError, submit_application

        v = _store()
        try:
            app_id = v.record_application(
                job_fingerprint="fp-nopayload", job_id=4, company="Acme",
                role="Engineer", platform="tanqeeb:egypt",
                job_url="https://example.com/j4",
                status=STATUS_APPROVED,          # no payload at all
            )
            with self.assertRaises(ApplyError):
                submit_application(app_id, v, notifier=None, dry_run=True)
        finally:
            v.close()

    def test_the_guard_runs_before_any_browser_is_launched(self):
        """Ordering is the whole point: refuse first, never launch and check."""
        import auto_apply.browser as browser
        from auto_apply.engine import ApplyError, submit_application

        launched = []

        def tripwire(*args, **kwargs):
            launched.append(1)
            raise AssertionError("a browser was launched for an unverified form")

        real = browser.browser_page
        browser.browser_page = tripwire
        v = _store()
        try:
            app_id = v.record_application(
                job_fingerprint="fp-order", job_id=5, company="Acme",
                role="Engineer", platform="tanqeeb:egypt",
                job_url="https://example.com/j5",
                payload={"fields": {"[name=\"keywords\"]": "IT support"}},
                status=STATUS_APPROVED,
            )
            with self.assertRaises(ApplyError):
                submit_application(app_id, v, notifier=None, dry_run=False)
            self.assertEqual(launched, [])
        finally:
            browser.browser_page = real
            v.close()


class TestAppPasswordNormalisation(unittest.TestCase):
    """Gmail rejects an app password containing an invisible NBSP.

    Google renders app passwords as four spaced groups. Copy-pasting from that
    page can carry a non-breaking or zero-width space, which is visually
    identical to a normal one and which Gmail refuses -- producing exactly the
    same "Invalid credentials" error as a genuinely wrong password. Normalising
    against a character class rather than `.strip()` removes them.
    """

    def setUp(self):
        from auto_apply.email_listener import _password_variants

        self.v = _password_variants
        self.expected = "abcdefghijklmnop"

    def test_every_surface_form_normalises_to_the_same_credential(self):
        nbsp, narrow, zwsp = chr(0x00a0), chr(0x202f), chr(0x200b)
        for label, raw in {
            "unspaced": self.expected,
            "spaced": "abcd efgh ijkl mnop",
            "trailing newline": self.expected + chr(10),
            "leading/trailing space": "  " + self.expected + "  ",
            "NBSP separators": nbsp.join(["abcd", "efgh", "ijkl", "mnop"]),
            "narrow NBSP": narrow.join(["abcd", "efgh", "ijkl", "mnop"]),
            "zero-width lurker": "abcdefgh" + zwsp + "ijklmnop",
        }.items():
            self.assertEqual(self.v(raw)[0], self.expected, f"form={label}")

    def test_spaced_form_is_also_offered(self):
        """Gmail accepts the spaced form too; try it as a fallback."""
        self.assertIn("abcd efgh ijkl mnop", self.v(self.expected))

    def test_variants_are_deduplicated_and_ordered(self):
        out = self.v(self.expected)
        self.assertEqual(len(out), len(set(out)))
        self.assertEqual(out[0], self.expected, "most likely form must be first")

    def test_empty_input_yields_nothing(self):
        self.assertEqual(self.v(""), [])


class TestInboxFailsSafely(unittest.TestCase):
    def test_missing_credentials_raise_a_useful_message(self):
        """Connecting is the backend's job now, not the monitor's."""
        from auto_apply.email_listener import ImapBackend

        backend = ImapBackend(host="imap.gmail.com", port=993, mailbox="INBOX",
                              user="", password="")
        with self.assertRaises(RuntimeError) as ctx:
            backend._connect()
        message = str(ctx.exception)
        self.assertIn("JOB_EMAIL", message)
        self.assertIn("auth_gmail.py", message,
                      "the error should point at the supported route")

    def test_a_failed_pass_returns_zero_counts_rather_than_crashing(self):
        """A dead mailbox must not take down a --daemon or --live process."""
        from auto_apply.email_listener import EmailMonitor

        v = _store()
        try:
            monitor = EmailMonitor(v, notifier=None)
            monitor.fetch_unread = lambda: (_ for _ in ()).throw(
                RuntimeError("auth failed")
            )
            counts = monitor.run_once()
            self.assertEqual(counts["scanned"], 0)
            self.assertEqual(counts["alerted"], 0)
        finally:
            v.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
