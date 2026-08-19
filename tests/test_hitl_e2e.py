"""
The whole conversational loop, end to end.

    draft  ->  card on BOTH channels  ->  "تعديل 1 الراتب: 18000"
           ->  updated card on BOTH channels  ->  "done 1"
           ->  Playwright fills the EDITED values  ->  screenshot
           ->  confirmation + evidence on BOTH channels  ->  status: submitted

Each of those steps is covered in isolation elsewhere. What is covered ONLY
here is that they compose -- and composition is where this feature can fail
invisibly:

  * The id printed on the card has to be the id "done 1" resolves against.
  * The value typed into the form has to be the EDITED one, not the original.
    An edit that updates only the displayed draft passes every unit test and
    submits the wrong salary.
  * The screenshot captured at submit time has to reach the confirmation card.
  * A message the bot itself wrote must never re-enter the loop.

Playwright and Gemini are faked. The engine, the vault, the card renderers, the
command grammar and the controller are all real.

Run:  python -m pytest tests/test_hitl_e2e.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto_apply.browser as browser_mod                         # noqa: E402
import auto_apply.candidate as candidate_mod                     # noqa: E402
import auto_apply.engine as engine_mod                           # noqa: E402
from auto_apply.browser import FormField                         # noqa: E402
from auto_apply.candidate import CandidateProfile                # noqa: E402
from auto_apply.control import ReviewController, TelegramCommandListener  # noqa: E402
from auto_apply.engine import prepare_application                # noqa: E402
from auto_apply.review import BOT_MARK                           # noqa: E402
from conftest import RecordingNotifier                           # noqa: E402
from config import settings                                      # noqa: E402
from models import Evaluation                                    # noqa: E402
from vault import STATUS_REVIEW, STATUS_SUBMITTED, SecureStore   # noqa: E402

FORM = [
    FormField(selector="#cv", kind="resume", input_type="file",
              label="Upload CV", required=False),
    FormField(selector="#email", kind="email", input_type="text",
              label="Email address"),
    FormField(selector="#phone", kind="phone", input_type="text",
              label="Mobile number"),
    FormField(selector="#cover", kind="cover_letter", input_type="textarea",
              label="Why do you want this role?"),
    FormField(selector="#salary", kind="salary", input_type="text",
              label="Expected salary"),
    FormField(selector="#sip", kind="unknown", input_type="text",
              label="How many years have you used Asterisk?"),
]

DRAFT = {
    "cover_letter": "I have run Issabel and Asterisk PBX estates for three "
                    "years, including SIP trunk migration.",
    "answers": [
        {"question": "How many years have you used Asterisk?",
         "answer": "Three years.", "confident": True},
    ],
    "salary_expectation": "12,000 AED",
    "_model": "fake-model",
}

PROFILE = CandidateProfile(
    full_name="Hossam Eldefrawy", email="h@example.com",
    phone="+201000000000", location="Cairo, Egypt", years_experience=3.0,
)


class _FakeInput:
    def __init__(self, value):
        self._value = value

    def input_value(self):
        return self._value

    def is_checked(self):
        return bool(self._value)


class _FakeControl:
    """A submit control as `click_submit` examines them now: visible, not the
    site search's button, not "Save and Apply later"."""

    def __init__(self, page, selector):
        self._page, self._selector = page, selector

    def inner_text(self):
        return "Submit"

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def get_attribute(self, _name):
        return None

    def scroll_into_view_if_needed(self, **kwargs):
        return None

    def evaluate(self, _script, *args):
        return False

    def click(self, **kwargs):
        self._page.clicked.append(self._selector)
        # Only a SUBMIT counts as submitting. Clicking "Apply Now" to open the
        # form is navigation, and conflating the two would let a test pass
        # that had merely clicked its way onto the page.
        if "submit" in self._selector.lower():
            self._page.submitted = True


class FakePage:
    def __init__(self):
        self.visited: list[str] = []
        self.clicked: list[str] = []
        self.uploaded: list[tuple[str, str]] = []
        # The submit flow re-reads the live DOM to confirm every question was
        # answered and that the board actually acknowledged the application.
        self.values: dict[str, str] = {}
        self.submitted = False

    def goto(self, url, **kwargs):
        self.visited.append(url)

    def fill(self, selector, value, timeout=None):
        self.values[selector] = value

    def set_input_files(self, selector, value, timeout=None):
        self.uploaded.append((selector, value))

    def query_selector(self, selector):
        if selector in self.values:
            return _FakeInput(self.values[selector])
        return object() if "submit" in selector.lower() else None

    def query_selector_all(self, selector):
        if "submit" not in selector.lower() and "Apply" not in selector:
            return []
        return [_FakeControl(self, selector)]

    def inner_text(self, _selector):
        if self.submitted:
            return "Application submitted"
        return "We need a VoIP engineer with SIP and Asterisk."

    def content(self):
        return "<form><input name='email'></form>"

    def click(self, selector, **kwargs):
        if "submit" not in selector.lower() and "Apply" not in selector:
            raise RuntimeError(f"no such element: {selector}")
        self.clicked.append(selector)
        self.submitted = True

    def wait_for_load_state(self, *args, **kwargs):
        pass


class HitlE2E(unittest.TestCase):
    def setUp(self):
        self.store = SecureStore(Path(tempfile.mkdtemp()) / "vault.db")
        self.notifier = RecordingNotifier()
        self.page = FakePage()
        self.filled: list[tuple[str, str]] = []
        self.shots: list[str] = []

        self.cv = Path(tempfile.mkdtemp()) / "master_cv.pdf"
        self.cv.write_bytes(b"%PDF-1.4\n% test CV\n")
        self._cv_env = os.environ.get("CV_PATH")
        os.environ["CV_PATH"] = str(self.cv)
        self._cv_chain = settings.raw.setdefault("cv", {}).get("paths")
        settings.raw["cv"]["paths"] = []

        # The suite runs with DRY_RUN=true, which makes `submit_application`
        # return True without touching the page -- so the whole point of this
        # file (that the EDITED values are what get typed) would be untestable.
        # Turning it off here is safe: the browser is faked, the notifier is a
        # recorder, and conftest blocks every non-loopback socket underneath.
        self._dry_run = settings.dry_run
        settings.dry_run = False

        @contextmanager
        def fake_browser_page(platform="default", headed=None):
            yield self.page

        def fake_fill_field(page, field_, value):
            self.filled.append((field_.selector, value))
            # Leave the value ON the page: the submit flow re-reads the live
            # DOM to confirm each question was answered, and a stub that only
            # reports success would fake past that check.
            page.values[field_.selector] = value
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
        browser_mod.inspect_form = lambda page, *a, **k: list(FORM)
        browser_mod.fill_field = fake_fill_field
        browser_mod.capture_evidence = fake_capture
        engine_mod.draft_answers = lambda ev, jd, qs: json.loads(json.dumps(DRAFT))
        candidate_mod.load_candidate = lambda *a, **k: PROFILE

        # The controller drives the REAL engine here -- that is the point.
        self.controller = ReviewController(self.store, self.notifier)
        self.listener = TelegramCommandListener(
            self.controller, owner_id=None, max_age_minutes=0
        )

    def tearDown(self):
        if self._cv_env is None:
            os.environ.pop("CV_PATH", None)
        else:
            os.environ["CV_PATH"] = self._cv_env
        settings.raw["cv"]["paths"] = self._cv_chain
        settings.dry_run = self._dry_run
        browser_mod.browser_page = self._saved["browser_page"]
        browser_mod.inspect_form = self._saved["inspect_form"]
        browser_mod.fill_field = self._saved["fill_field"]
        browser_mod.capture_evidence = self._saved["capture_evidence"]
        engine_mod.draft_answers = self._saved["draft_answers"]
        candidate_mod.load_candidate = self._saved["load_candidate"]
        self.store.close()

    @staticmethod
    def _ev(fingerprint="fp-hitl"):
        return Evaluation(
            fingerprint=fingerprint, company_name="Etisalat",
            role_title="VoIP Engineer", location="Dubai, UAE", match_score=95,
            source_platform="tanqeeb:uae",
            direct_link="https://uae.tanqeeb.com/jobs/021136159.html",
            why_matched="Issabel PBX and SIP trunk overlap.", ref_id=101,
        )

    def _filled(self, selector):
        return dict(self.filled).get(selector)


# ---------------------------------------------------------------------------
class TestTheConversationalLoop(HitlE2E):
    def test_draft_edit_confirm_submit(self):
        # -- 1. a draft is prepared and pushed to BOTH channels -------------
        app_id = prepare_application(self._ev(), self.store, self.notifier)
        self.assertIsNotNone(app_id)
        self.assertEqual(self.store.get_application(app_id)["status"],
                         STATUS_REVIEW)
        self.assertEqual(len(self.notifier.telegram), 1)
        self.assertEqual(len(self.notifier.whatsapp), 1)

        card = self.notifier.last_telegram
        self.assertIn(f"[DRAFT #{app_id}]", card)
        self.assertIn("Etisalat", card)
        self.assertIn("VoIP Engineer", card)
        self.assertIn("tanqeeb:uae", card)
        self.assertIn("95%", card)
        self.assertIn("12,000 AED", card)
        self.assertIn("3 years", card)
        self.assertIn("Issabel", card)
        self.assertIn(f"done {app_id}", card)
        # The WhatsApp card carries the same reference and no URL.
        self.assertIn(f"[DRAFT #{app_id}]", self.notifier.last_whatsapp)
        self.assertNotIn("http", self.notifier.last_whatsapp)

        # -- 2. the user replies in Arabic to change the salary -------------
        self.notifier.clear()
        reply = self.listener.handle_message(f"تعديل {app_id} الراتب: 18000 درهم")
        self.assertIsNotNone(reply)
        self.assertTrue(reply.ok, reply.detail)
        self.assertEqual(len(self.notifier.telegram), 1,
                         "the updated draft was not re-sent")
        self.assertIn("18000 درهم", self.notifier.last_telegram)
        self.assertIn("18000 درهم", self.notifier.last_whatsapp)
        self.assertIn("1 edit(s) applied", self.notifier.last_telegram)

        # -- 3. and rewrites the cover letter in English --------------------
        self.notifier.clear()
        new_letter = "I maintain Issabel PBX for 4,000 extensions."
        self.listener.handle_message(
            f"edit {app_id} cover letter: {new_letter}"
        )
        app = self.store.get_application(app_id)
        self.assertEqual(app["cover_letter_text"], new_letter)
        self.assertEqual(app["status"], STATUS_REVIEW,
                         "an edited draft must wait for a fresh confirmation")
        self.assertEqual(app["revision"], 2)

        # -- 4. "done" submits it -------------------------------------------
        self.notifier.clear()
        reply = self.listener.handle_message(f"done {app_id}")
        self.assertIsNotNone(reply)
        self.assertTrue(reply.ok, reply.detail)

        # -- 5. the EDITED values are what went into the form ---------------
        self.assertEqual(
            self._filled("#salary"), "18000 درهم",
            "the ORIGINAL salary was submitted -- the edit was cosmetic",
        )
        self.assertEqual(
            self._filled("#cover"), new_letter,
            "the ORIGINAL cover letter was submitted",
        )
        self.assertEqual(self._filled("#email"), "h@example.com")
        self.assertEqual([u[1] for u in self.page.uploaded], [str(self.cv)],
                         "the CV was not attached to the form")
        self.assertEqual(self._filled("#sip"), "Three years.")
        self.assertTrue(self.page.clicked, "the submit button was never clicked")

        # -- 6. evidence, status and the confirmation on both channels ------
        final = self.store.get_application(app_id)
        self.assertEqual(final["status"], STATUS_SUBMITTED)
        self.assertTrue(final["submitted_at"])
        self.assertEqual(final["screenshot_path"], self.shots[-1])

        self.assertIn("APPLICATION SUCCESSFULLY SUBMITTED",
                      self.notifier.last_telegram)
        self.assertIn("APPLICATION SUBMITTED", self.notifier.last_whatsapp)
        self.assertIn(f"[DRAFT #{app_id}]", self.notifier.last_whatsapp)
        self.assertEqual([p[0] for p in self.notifier.photos],
                         [final["screenshot_path"]],
                         "the evidence screenshot never reached Telegram")

    def test_the_bot_never_answers_its_own_cards(self):
        """Every card it emits contains "done <id>" as instructions."""
        app_id = prepare_application(self._ev(), self.store, self.notifier)
        for card in self.notifier.telegram + self.notifier.whatsapp:
            with self.subTest(card=card[:40]):
                self.assertIn(BOT_MARK, card)
                self.assertIsNone(self.listener.handle_message(card))
        self.assertEqual(self.store.get_application(app_id)["status"],
                         STATUS_REVIEW)
        self.assertEqual(self.page.clicked, [])

    def test_a_bare_done_works_when_exactly_one_draft_is_waiting(self):
        app_id = prepare_application(self._ev(), self.store, self.notifier)
        self.listener.handle_message("موافق")
        self.assertEqual(self.store.get_application(app_id)["status"],
                         STATUS_SUBMITTED)

    def test_a_declined_draft_is_never_submitted_by_a_later_done(self):
        app_id = prepare_application(self._ev(), self.store, self.notifier)
        self.listener.handle_message(f"رفض {app_id}")
        self.listener.handle_message(f"done {app_id}")
        self.assertEqual(self.store.get_application(app_id)["status"],
                         "declined")
        self.assertEqual(self.page.clicked, [])

    def test_an_edit_after_submission_is_refused_and_changes_nothing(self):
        app_id = prepare_application(self._ev(), self.store, self.notifier)
        self.listener.handle_message(f"done {app_id}")
        before = self.store.get_application(app_id)
        self.listener.handle_message(f"edit {app_id} salary: 99999")
        after = self.store.get_application(app_id)
        self.assertEqual(after["submitted_payload_json"],
                         before["submitted_payload_json"])
        self.assertEqual(after["status"], STATUS_SUBMITTED)

    def test_status_reflects_the_draft_through_its_whole_life(self):
        app_id = prepare_application(self._ev(), self.store, self.notifier)

        self.notifier.clear()
        self.listener.handle_message("status")
        self.assertIn(f"[DRAFT #{app_id}]", self.notifier.last_telegram)
        self.assertIn("Submitted so far: 0", self.notifier.last_telegram)

        self.listener.handle_message(f"done {app_id}")
        self.notifier.clear()
        self.listener.handle_message("الحالة")
        self.assertIn("Nothing is waiting", self.notifier.last_telegram)
        self.assertIn("Submitted so far: 1", self.notifier.last_telegram)

    def test_a_search_widget_page_is_refused_but_the_letter_is_kept(self):
        """The card says apply by hand, and "done" does not override that."""
        browser_mod.inspect_form = lambda page, *a, **k: [
            FormField(selector='[name="keywords"]', kind="unknown",
                      input_type="text", label="keywords | Search jobs"),
            FormField(selector='[name="state"]', kind="unknown",
                      input_type="select", label="state | Governorate"),
        ]
        app_id = prepare_application(self._ev("fp-widget"), self.store,
                                     self.notifier)
        card = self.notifier.last_telegram
        self.assertIn("NO AUTO-SUBMITTABLE FORM FOUND", card)
        self.assertIn("uae.tanqeeb.com", card)
        self.assertTrue(self.store.get_application(app_id)["cover_letter_text"])

        self.notifier.clear()
        reply = self.listener.handle_message(f"done {app_id}")
        self.assertFalse(reply.ok)
        # Trying "Apply Now" to reach a form is fine -- pressing SUBMIT on the
        # site's search widget is the thing that must never happen.
        self.assertFalse(self.page.submitted,
                         "a search widget was submitted as an application")
        self.assertEqual(
            [c for c in self.page.clicked if "submit" in c.lower()], [])
        self.assertEqual(self.store.get_application(app_id)["status"], "failed")
        self.assertIn("APPLICATION FAILED", self.notifier.last_telegram)
        self.assertIn("APPLICATION FAILED", self.notifier.last_whatsapp)

    def test_a_captcha_stops_the_submission_before_the_click(self):
        """Clicking under an unsolved challenge banks an error page as success."""
        self.page.content = lambda: '<div class="g-recaptcha" data-sitekey="x">'
        app_id = prepare_application(self._ev("fp-captcha"), self.store,
                                     self.notifier)
        self.notifier.clear()
        reply = self.listener.handle_message(f"done {app_id}")
        self.assertFalse(reply.ok)
        self.assertEqual(self.page.clicked, [])
        app = self.store.get_application(app_id)
        self.assertEqual(app["status"], "failed")
        self.assertIn("reCAPTCHA", app["failure_reason"])
        self.assertIn("reCAPTCHA", self.notifier.last_telegram)

    def test_a_linkedin_match_is_never_drafted_at_all(self):
        """The exclusion is at the top of the entry point, not downstream."""
        ev = self._ev("fp-linkedin")
        ev.source_platform = "linkedin"
        self.assertIsNone(prepare_application(ev, self.store, self.notifier))
        self.assertEqual(self.notifier.telegram, [])
        self.assertEqual(self.page.visited, [])

    def test_a_free_form_note_is_kept_when_ai_revision_is_off(self):
        """With `hitl.allow_ai_revision: false` the instruction is still the
        user's decision, so it is recorded rather than dropped.

        The model is not called at all here -- deliberately. Reaching Gemini is
        the one step that cannot run offline, and its failure path is covered
        by the unit test for `revise_cover_letter`.
        """
        original = dict(settings.raw.get("hitl", {}) or {})
        settings.raw["hitl"] = dict(original, allow_ai_revision=False)
        try:
            app_id = prepare_application(self._ev(), self.store, self.notifier)
            self.notifier.clear()
            reply = self.listener.handle_message(
                f"edit {app_id}: make it shorter and mention Issabel"
            )
        finally:
            settings.raw["hitl"] = original
        self.assertTrue(reply.ok)
        stored = json.loads(
            self.store.get_application(app_id)["submitted_payload_json"]
        )
        self.assertEqual(stored["notes"],
                         ["make it shorter and mention Issabel"])
        self.assertIn("YOUR NOTES", self.notifier.last_telegram)


if __name__ == "__main__":
    unittest.main(verbosity=2)
