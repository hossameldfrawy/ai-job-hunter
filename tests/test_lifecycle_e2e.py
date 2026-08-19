"""
The whole auto-apply lifecycle, end to end, with the browser and Gemini stubbed.

    --register  ->  --apply  ->  --applications  ->  --approve  ->  evidence

Each stage is covered somewhere in the other files; what is NOT covered
anywhere else is that they compose -- that the id `--apply` prints is the one
`--approve` accepts, that the credentials `--register` vaults come back out
decryptable, and that the screenshot captured at submit time survives into the
Telegram confirmation. Those are exactly the seams a refactor breaks.

WHAT IS FAKED, AND WHAT IS DELIBERATELY NOT
-------------------------------------------
Faked: Playwright (`browser_page`, `inspect_form`, `fill_field`,
`capture_evidence`), Gemini (`draft_answers`), and the CV extraction. All three
are I/O and all three are tested for real elsewhere.

NOT faked: `looks_like_application_form`. It is the safety decision that
separates a genuine application from filling the site's search box and calling
it a submission, so a fake would test nothing worth testing.

Run:  python -m pytest tests/test_lifecycle_e2e.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto_apply.browser as browser_mod                       # noqa: E402
import auto_apply.candidate as candidate_mod                   # noqa: E402
import auto_apply.engine as engine_mod                         # noqa: E402
import auto_apply.profile_builder as pb_mod                    # noqa: E402
from auto_apply.browser import FormField                       # noqa: E402
from auto_apply.candidate import CandidateProfile              # noqa: E402
from auto_apply.engine import (                                # noqa: E402
    ApplyError, approve, decline, is_automatable, prepare_application,
    submit_application,
)
from auto_apply.profile_builder import Platform, prefill_registration  # noqa: E402
from models import Evaluation                                  # noqa: E402
from vault import (                                            # noqa: E402
    STATUS_APPROVED, STATUS_DECLINED, STATUS_REVIEW, STATUS_SUBMITTED,
    SecureStore,
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------
class FakePage:
    """Just enough Playwright surface for the two flows that use it."""

    def __init__(self, body: str = "We need a VoIP engineer with SIP."):
        self.body = body
        self.visited: list[str] = []
        self.clicked: list[str] = []

    def goto(self, url, **kwargs):
        self.visited.append(url)

    def inner_text(self, _selector):
        return self.body

    def click(self, selector, **kwargs):
        if "submit" not in selector.lower() and "Apply" not in selector:
            raise RuntimeError(f"no such element: {selector}")
        self.clicked.append(selector)

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def wait_for_timeout(self, *args, **kwargs):
        pass


#: A real application form: CV upload + cover letter + personal details.
REAL_FORM = [
    FormField(selector="#cv", kind="resume", input_type="file", label="Upload CV"),
    FormField(selector="#email", kind="email", input_type="text", label="Email"),
    FormField(selector="#phone", kind="phone", input_type="text", label="Phone"),
    FormField(selector="#name", kind="full_name", input_type="text",
              label="Full name"),
    FormField(selector="#cover", kind="cover_letter", input_type="textarea",
              label="Why do you want this role?"),
    FormField(selector="#notice", kind="unknown", input_type="text",
              label="What is your notice period in weeks?"),
]

#: The site's search widget, which is NOT an application form.
SEARCH_WIDGET = [
    FormField(selector='[name="keywords"]', kind="unknown", input_type="text",
              label="keywords | Search jobs"),
    FormField(selector='[name="state"]', kind="unknown", input_type="select",
              label="state | Governorate"),
]

FAKE_DRAFT = {
    "cover_letter": "I have run Issabel and Asterisk PBX estates for three "
                    "years, including SIP trunk migration.",
    "answers": [
        {"question": "What is your notice period in weeks?",
         "answer": "Two weeks.", "confident": True},
    ],
    "salary_expectation": "",
    "_model": "fake-model",
}

PROFILE = CandidateProfile(
    full_name="Hossam Eldefrawy", email="hossam.eldefrawy.dev@gmail.com",
    phone="+201000000000", location="Cairo, Egypt",
    current_title="IT Support Engineer", years_experience=3.0,
)


class RecordingNotifier:
    def __init__(self):
        self.sent: list[str] = []

    def send_via_telegram(self, message):
        self.sent.append(str(message))
        return True, "sent"


class LifecycleHarness(unittest.TestCase):
    """Patches the I/O boundaries for the whole class, restores them after."""

    form: list[FormField] = REAL_FORM

    def setUp(self):
        self.v = SecureStore(Path(tempfile.mkdtemp()) / "vault.db")
        self.notifier = RecordingNotifier()
        self.page = FakePage()
        self.filled: list[tuple[str, str]] = []
        self.shots: list[str] = []

        @contextmanager
        def fake_browser_page(platform="default", headed=None):
            yield self.page

        def fake_fill_field(page, field_, value):
            self.filled.append((field_.kind, value))
            return True

        def fake_capture(page, prefix):
            path = str(Path(tempfile.gettempdir()) / f"{prefix}.png")
            self.shots.append(path)
            return path

        self._saved = {
            "browser_page": browser_mod.browser_page,
            "inspect_form": browser_mod.inspect_form,
            "fill_field": browser_mod.fill_field,
            "capture_evidence": browser_mod.capture_evidence,
            "draft_answers": engine_mod.draft_answers,
            "load_candidate": candidate_mod.load_candidate,
        }
        browser_mod.browser_page = fake_browser_page
        browser_mod.inspect_form = lambda page, *a, **k: list(self.form)
        browser_mod.fill_field = fake_fill_field
        browser_mod.capture_evidence = fake_capture
        engine_mod.draft_answers = lambda ev, jd, qs: dict(FAKE_DRAFT)
        candidate_mod.load_candidate = lambda *a, **k: PROFILE

    def tearDown(self):
        browser_mod.browser_page = self._saved["browser_page"]
        browser_mod.inspect_form = self._saved["inspect_form"]
        browser_mod.fill_field = self._saved["fill_field"]
        browser_mod.capture_evidence = self._saved["capture_evidence"]
        engine_mod.draft_answers = self._saved["draft_answers"]
        candidate_mod.load_candidate = self._saved["load_candidate"]
        self.v.close()

    @staticmethod
    def _ev(source="tanqeeb:egypt", fingerprint="fp-e2e") -> Evaluation:
        return Evaluation(
            fingerprint=fingerprint, company_name="Erada Egypt",
            role_title="IT Help Desk Specialist", location="Qena, Egypt",
            match_score=95, source_platform=source,
            direct_link="https://egypt.tanqeeb.com/jobs/021136159.html",
            why_matched="Issabel PBX and Active Directory overlap.",
            ref_id=101,
        )


# ---------------------------------------------------------------------------
class TestHappyPath(LifecycleHarness):
    """register -> apply -> applications -> approve -> submit -> evidence."""

    def test_the_full_lifecycle(self):
        # -- 1. --register -------------------------------------------------
        platform = Platform(name="Tanqeeb", url="https://www.tanqeeb.com/register")
        report = prefill_registration(
            platform, self.v, self.notifier, headed=False
        )
        self.assertIn("Tanqeeb", [p["platform_name"]
                                  for p in self.v.list_platforms()])
        self.assertTrue(report["filled"], "nothing was pre-filled")

        creds = self.v.get_credentials("Tanqeeb")
        self.assertTrue(creds["password"], "the vault returned no password")
        self.assertGreaterEqual(len(creds["password"]), 12)

        # -- 2. --apply ----------------------------------------------------
        ev = self._ev()
        app_id = prepare_application(ev, self.v, self.notifier)
        self.assertIsNotNone(app_id)

        app = self.v.get_application(app_id)
        self.assertEqual(app["status"], STATUS_REVIEW,
                         "a draft must wait for a human, not self-approve")
        self.assertIn("Issabel", app["cover_letter_text"])
        stored = json.loads(app["submitted_payload_json"])
        self.assertIs(stored["form_ok"], True)

        # -- 3. --applications ---------------------------------------------
        pending = self.v.applications_by_status(STATUS_REVIEW)
        self.assertEqual([r["id"] for r in pending], [app_id],
                         "the dashboard must show the id --approve expects")

        # -- 4. --approve ---------------------------------------------------
        approve(app_id, self.v)
        self.assertEqual(self.v.get_application(app_id)["status"],
                         STATUS_APPROVED)

        ok = submit_application(app_id, self.v, self.notifier, dry_run=False)
        self.assertTrue(ok)

        # -- 5. evidence ----------------------------------------------------
        final = self.v.get_application(app_id)
        self.assertEqual(final["status"], STATUS_SUBMITTED)
        self.assertTrue(final["submitted_at"], "no submission timestamp")
        self.assertTrue(final["screenshot_path"], "no screenshot recorded")
        self.assertEqual(len(self.shots), 2,
                         "expected a registration and a submission screenshot")
        self.assertTrue(self.page.clicked, "the submit button was never clicked")

        # -- 6. the messages the user actually sees -------------------------
        joined = "\n".join(self.notifier.sent)
        self.assertIn("NEW PLATFORM ACCOUNT CREATED", joined)
        self.assertIn("APPLICATION DRAFT READY FOR REVIEW", joined)
        self.assertIn("APPLICATION SUCCESSFULLY SUBMITTED", joined)
        self.assertIn(f"--approve {app_id}", joined,
                      "the review card must quote the runnable command")
        self.assertIn("Screenshot saved", joined)

    def test_the_cv_is_uploaded_and_the_cover_letter_is_placed(self):
        ev = self._ev()
        app_id = prepare_application(ev, self.v, self.notifier)
        approve(app_id, self.v)
        submit_application(app_id, self.v, self.notifier, dry_run=False)

        kinds = [k for k, _ in self.filled]
        self.assertIn("resume", kinds, "the CV was never attached")
        self.assertIn("cover_letter", kinds)
        cover = dict((k, v) for k, v in self.filled)["cover_letter"]
        self.assertIn("Issabel", cover)

    def test_a_screening_question_is_answered_from_the_draft(self):
        ev = self._ev()
        app_id = prepare_application(ev, self.v, self.notifier)
        stored = json.loads(
            self.v.get_application(app_id)["submitted_payload_json"]
        )
        self.assertEqual(stored["fields"].get("#notice"), "Two weeks.")


# ---------------------------------------------------------------------------
class TestDraftingIsIdempotent(LifecycleHarness):
    """Re-running --apply must not produce a second draft or a second alert."""

    def test_the_same_job_drafts_once(self):
        ev = self._ev()
        first = prepare_application(ev, self.v, self.notifier)
        sent_after_first = len(self.notifier.sent)

        for _ in range(3):
            again = prepare_application(self._ev(), self.v, self.notifier)
            self.assertEqual(again, first, "a duplicate application was created")

        self.assertEqual(
            len(self.notifier.sent), sent_after_first,
            "re-running --apply re-sent the review card for a job already "
            "waiting in the review queue",
        )
        self.assertEqual(len(self.v.applications_by_status(STATUS_REVIEW)), 1)


# ---------------------------------------------------------------------------
class TestLinkedInIsNeverAutomated(LifecycleHarness):
    """Excluded at EVERY entry point, not filtered somewhere downstream."""

    def test_is_automatable_refuses_every_linkedin_spelling(self):
        for source in ("linkedin", "LinkedIn", "linkedin:gcc", "LINKEDIN:uae"):
            with self.subTest(source=source):
                allowed, why = is_automatable(source)
                self.assertFalse(allowed)
                self.assertIn("never automated", why)

    def test_drafting_refuses_and_opens_no_browser(self):
        ev = self._ev(source="linkedin", fingerprint="fp-li")
        self.assertIsNone(prepare_application(ev, self.v, self.notifier))
        self.assertEqual(self.page.visited, [],
                         "a browser navigated to a LinkedIn posting")
        self.assertEqual(self.notifier.sent, [])
        self.assertIsNone(self.v.application_for_fingerprint("fp-li"))

    def test_submission_refuses_even_if_a_row_somehow_exists(self):
        """Defence in depth: the gate must not rely on drafting having run."""
        app_id = self.v.record_application(
            job_fingerprint="fp-li-2", job_id=1, company="OBT Group",
            role="IT Support Specialist - UAE", platform="linkedin",
            job_url="https://www.linkedin.com/jobs/view/123",
            payload={"fields": {"#email": "a@b.c"}, "form_ok": True},
            status=STATUS_APPROVED,
        )
        with self.assertRaises(ApplyError) as ctx:
            submit_application(app_id, self.v, self.notifier, dry_run=False)
        self.assertIn("never automated", str(ctx.exception))
        self.assertEqual(self.page.visited, [])

    def test_non_linkedin_boards_are_still_allowed(self):
        for source in ("tanqeeb:egypt", "talent:ae", "rss:Jobicy", "api:arbeitnow"):
            with self.subTest(source=source):
                self.assertTrue(is_automatable(source)[0])


# ---------------------------------------------------------------------------
class TestSearchWidgetIsRefused(LifecycleHarness):
    """The failure mode that looks like success."""

    form = SEARCH_WIDGET

    def test_drafting_records_the_refusal_but_keeps_the_letter(self):
        ev = self._ev(fingerprint="fp-widget")
        app_id = prepare_application(ev, self.v, self.notifier)

        stored = json.loads(
            self.v.get_application(app_id)["submitted_payload_json"]
        )
        self.assertIs(stored["form_ok"], False)
        self.assertIn("search", stored["form_note"].lower())
        self.assertTrue(
            self.v.get_application(app_id)["cover_letter_text"],
            "the letter is the salvageable part -- it must still be saved",
        )

    def test_the_review_card_says_to_apply_by_hand(self):
        app_id = prepare_application(self._ev(fingerprint="fp-widget2"),
                                     self.v, self.notifier)
        self.assertIsNotNone(app_id)
        card = self.notifier.sent[-1]
        self.assertIn("No auto-submittable form found", card)
        self.assertIn("egypt.tanqeeb.com", card,
                      "the card must carry the link to apply manually")

    def test_submission_is_refused_without_launching_a_browser(self):
        app_id = prepare_application(self._ev(fingerprint="fp-widget3"),
                                     self.v, self.notifier)
        approve(app_id, self.v)
        visited_before = len(self.page.visited)

        with self.assertRaises(ApplyError) as ctx:
            submit_application(app_id, self.v, self.notifier, dry_run=False)
        self.assertIn("apply by hand", str(ctx.exception).lower())
        self.assertEqual(
            len(self.page.visited), visited_before,
            "a browser was driven at a page with no application form",
        )


# ---------------------------------------------------------------------------
class TestHumanGate(LifecycleHarness):
    """Approval is a gate, not a notification."""

    def test_a_draft_cannot_be_submitted_before_approval(self):
        app_id = prepare_application(self._ev(fingerprint="fp-gate-e2e"),
                                     self.v, self.notifier)
        with self.assertRaises(ApplyError) as ctx:
            submit_application(app_id, self.v, self.notifier, dry_run=False)
        self.assertIn("approve", str(ctx.exception).lower())
        self.assertEqual(self.page.clicked, [])

    def test_declining_is_final(self):
        app_id = prepare_application(self._ev(fingerprint="fp-decline-e2e"),
                                     self.v, self.notifier)
        decline(app_id, self.v)
        self.assertEqual(self.v.get_application(app_id)["status"],
                         STATUS_DECLINED)
        with self.assertRaises(ApplyError):
            submit_application(app_id, self.v, self.notifier, dry_run=False)

    def test_a_dry_run_approval_touches_nothing(self):
        app_id = prepare_application(self._ev(fingerprint="fp-dry-e2e"),
                                     self.v, self.notifier)
        approve(app_id, self.v)
        self.assertTrue(
            submit_application(app_id, self.v, self.notifier, dry_run=True)
        )
        self.assertEqual(self.page.clicked, [])
        self.assertEqual(self.v.get_application(app_id)["status"],
                         STATUS_APPROVED, "a dry run must not mark it submitted")


# ---------------------------------------------------------------------------
class TestFailureIsRecoverable(LifecycleHarness):
    """A failed submission keeps the work and says how to finish by hand."""

    def test_a_missing_submit_button_is_reported_not_swallowed(self):
        app_id = prepare_application(self._ev(fingerprint="fp-nobutton"),
                                     self.v, self.notifier)
        approve(app_id, self.v)

        def no_button(selector, **kwargs):
            raise RuntimeError("element not found")

        self.page.click = no_button
        self.assertFalse(
            submit_application(app_id, self.v, self.notifier, dry_run=False)
        )
        app = self.v.get_application(app_id)
        self.assertEqual(app["status"], "failed")
        self.assertIn("submit button", (app["failure_reason"] or "").lower())
        self.assertIn("APPLICATION #%d FAILED" % app_id,
                      "\n".join(self.notifier.sent))
        self.assertIn("egypt.tanqeeb.com", self.notifier.sent[-1],
                      "the failure notice must carry the manual link")

    def test_evidence_survives_a_later_status_change(self):
        """Re-approving a submitted application must not erase the proof."""
        app_id = prepare_application(self._ev(fingerprint="fp-evidence"),
                                     self.v, self.notifier)
        approve(app_id, self.v)
        submit_application(app_id, self.v, self.notifier, dry_run=False)
        shot = self.v.get_application(app_id)["screenshot_path"]
        self.assertTrue(shot)

        approve(app_id, self.v)          # a fat-fingered second --approve
        self.assertEqual(
            self.v.get_application(app_id)["screenshot_path"], shot,
            "the submission screenshot was wiped by a later status update",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
