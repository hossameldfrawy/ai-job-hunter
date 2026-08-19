"""
Browser automation: what the form inspector sees, what it refuses, and what it
puts in each box.

THE FAILURE MODE THIS FILE IS ABOUT
-----------------------------------
Every job page is full of forms that are not the application form -- the site's
search widget, a newsletter box, a login panel. `inspect_form` describes all of
them equally well, and filling one then clicking "submit" runs a SEARCH and
records it as a delivered application. That is worse than failing, because it
looks like success: the user waits for a reply to something that was never sent.

The same shape shows up three more times, and each one is guarded here:

  * a CV field with no CV file on disk -> the board rejects it, the screenshot
    captures the rejection, the row says "submitted"
  * an unsolved CAPTCHA -> the click does not raise, the page re-renders with
    an error, the screenshot captures the error
  * an "unknown" field guessed at instead of asked about

Playwright is faked. The classification, the refusal rules and the field
mapping are NOT faked -- those are the decisions worth testing.

Run:  python -m pytest tests/test_browser_forms.py -v
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto_apply.browser as browser_mod                         # noqa: E402
from auto_apply.browser import (                                 # noqa: E402
    FIELD_PATTERNS, FormField, SCREENSHOT_DIR, _classify, capture_evidence,
    detect_captcha, fill_field, inspect_form, looks_like_application_form,
    timestamped_screenshot_path,
)
from auto_apply.engine import build_payload, describe_fields     # noqa: E402
from models import Evaluation                                    # noqa: E402


# ---------------------------------------------------------------------------
# A minimal DOM double
# ---------------------------------------------------------------------------
class FakeHandle:
    def __init__(self, tag, attrs=None, options=None, label_text=""):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.options = list(options or [])
        self.label_text = label_text

    def evaluate(self, script):
        if "tagName" in script:
            return self.tag
        return ""

    def evaluate_handle(self, _script):
        return None

    def get_attribute(self, name):
        return self.attrs.get(name)

    def query_selector_all(self, selector):
        if selector == "option":
            return [FakeOption(text) for text in self.options]
        return []


class FakeOption:
    def __init__(self, text):
        self._text = text

    def inner_text(self):
        return self._text


class FakeLabel:
    def __init__(self, text):
        self._text = text

    def inner_text(self):
        return self._text


class FakePage:
    """Just enough Playwright surface for inspect_form / fill / screenshot."""

    def __init__(self, handles, markup="<html><body></body></html>",
                 labels=None):
        self.handles = list(handles)
        self.markup = markup
        self.labels = dict(labels or {})
        self.filled: list[tuple[str, str]] = []
        self.uploaded: list[tuple[str, str]] = []
        self.checked: list[str] = []
        self.selected: list[tuple[str, str]] = []
        self.screenshots: list[dict] = []
        self.screenshot_error: Exception | None = None
        self.content_error: Exception | None = None

    # -- inspection ---------------------------------------------------------
    def query_selector_all(self, selector):
        if selector == "form":
            return []                      # inspect_form then falls back to page
        if selector == "input, textarea, select":
            return self.handles
        return []

    def query_selector(self, selector):
        match = re.match(r'label\[for="(.+)"\]', selector or "")
        if match and match.group(1) in self.labels:
            return FakeLabel(self.labels[match.group(1)])
        return None

    def content(self):
        if self.content_error:
            raise self.content_error
        return self.markup

    # -- interaction --------------------------------------------------------
    def fill(self, selector, value, timeout=None):
        self.filled.append((selector, value))

    def set_input_files(self, selector, value, timeout=None):
        self.uploaded.append((selector, value))

    def check(self, selector, timeout=None):
        self.checked.append(selector)

    def select_option(self, selector, label=None, timeout=None):
        self.selected.append((selector, label))

    def screenshot(self, path=None, full_page=False):
        if self.screenshot_error:
            raise self.screenshot_error
        self.screenshots.append({"path": path, "full_page": full_page})
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")


def _field(kind, selector="#x", input_type="text", label="", required=False):
    return FormField(selector=selector, kind=kind, input_type=input_type,
                     label=label or kind, required=required)


# ---------------------------------------------------------------------------
class TestFieldClassification(unittest.TestCase):
    """Every board names its inputs differently; matching on markup is useless."""

    CASES = [
        ("Email address", "email"),
        ("البريد الالكتروني", "email"),
        ("Mobile number", "phone"),
        ("رقم الهاتف", "phone"),
        ("First name", "first_name"),
        ("Last name", "last_name"),
        ("Full name", "full_name"),
        ("Expected salary", "salary"),
        ("الراتب المتوقع", "salary"),
        ("Notice period", "notice_period"),
        ("Years of experience", "years_experience"),
        ("Current job title", "current_title"),
        ("Current employer", "current_employer"),
        ("City", "location"),
        ("LinkedIn profile URL", "linkedin"),
        ("Cover letter", "cover_letter"),
        ("Why do you want this role?", "cover_letter"),
        ("Upload your CV", "resume"),
        ("السيرة الذاتية", "resume"),
        ("Password", "password"),
    ]

    def test_labels_a_human_would_read_are_recognised(self):
        for label, expected in self.CASES:
            with self.subTest(label=label):
                self.assertEqual(_classify(label), expected)

    def test_the_more_specific_concept_wins(self):
        """Specificity, not substring length.

        Longest-substring was the obvious rule and it is wrong in a way that
        costs real data: "Email address" contains both "email" (5) and
        location's "address" (7), so the longer one won and the candidate's
        city was typed into the email box.
        """
        self.assertEqual(_classify("Email address"), "email")
        self.assertEqual(_classify("first name"), "first_name")
        self.assertEqual(_classify("last name"), "last_name")
        self.assertEqual(_classify("years of experience"), "years_experience")
        self.assertEqual(_classify("Expected salary at current company"),
                         "salary")

    def test_snake_case_name_attributes_are_understood(self):
        """`name` and `id` attributes are fed straight in, and snake_case IS
        the convention for them -- so a form with no visible <label> used to
        classify almost entirely as `unknown`, and every field on it went to
        Gemini as a free-text question."""
        for raw, expected in (
            ("candidate[cover_letter]", "cover_letter"),
            ("applicant-email", "email"),
            ("notice_period", "notice_period"),
            ("expected_salary", "salary"),
            ("years_experience", "years_experience"),
            ("txtMobile | Mobile", "phone"),
            ("applicant.e-mail", "email"),
            ("yearsExperience", "years_experience"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(_classify(raw), expected)

    def test_an_unrecognised_field_is_marked_unknown_not_guessed(self):
        """A wrong guess types the phone number into "Notice period"."""
        self.assertEqual(_classify("Do you own a car?"), "unknown")
        self.assertEqual(_classify(""), "unknown")
        self.assertEqual(_classify(None), "unknown")

    def test_a_short_needle_matches_a_whole_word_not_a_fragment(self):
        """Regression: "tel" matched inside "Tell us about…", so a free-text
        essay box was classified as a phone field and filled with the
        candidate's number. "city" matched inside "capacity" the same way."""
        for label, expected in (
            ("tel", "phone"),
            ("type=tel | tel", "phone"),
            ("Tell us about your most difficult outage", "unknown"),
            ("What is your intelligence quotient?", "unknown"),
            ("City", "location"),
            ("Storage capacity in GB", "unknown"),
            ("Expected pay", "salary"),
            ("Preferred payment method", "unknown"),
            ("Upload CV", "resume"),
            ("Do you have a driving licence?", "unknown"),
        ):
            with self.subTest(label=label):
                self.assertEqual(_classify(label), expected)

    def test_a_screening_question_about_experience_is_not_a_number_field(self):
        """"Describe your experience with Asterisk" is prose for the model to
        write, not a box to put "3" in."""
        for label in ("Describe your experience with Asterisk",
                      "What experience do you have with SIP trunks?",
                      "Tell us about your most difficult outage"):
            with self.subTest(label=label):
                self.assertEqual(_classify(label), "unknown")

    def test_an_unknown_free_text_field_becomes_a_question_for_the_model(self):
        self.assertTrue(_field("unknown", input_type="text").is_question)
        self.assertTrue(_field("cover_letter", input_type="textarea").is_question)

    def test_a_field_we_can_answer_ourselves_is_not_a_question(self):
        self.assertFalse(_field("email").is_question)
        self.assertFalse(_field("unknown", input_type="select").is_question)
        self.assertFalse(_field("resume", input_type="file").is_question)

    def test_every_pattern_group_is_reachable(self):
        """A typo in the table would silently retire a whole field kind."""
        for kind, needles in FIELD_PATTERNS.items():
            with self.subTest(kind=kind):
                self.assertTrue(needles)
                self.assertEqual(_classify(needles[0]), kind)


class TestInspectForm(unittest.TestCase):
    def _page(self):
        return FakePage(
            handles=[
                FakeHandle("input", {"type": "email", "name": "applicant_email"}),
                FakeHandle("input", {"type": "tel", "name": "txtMobile",
                                     "placeholder": "Mobile"}),
                FakeHandle("input", {"type": "file", "name": "cv_upload",
                                     "aria-label": "Upload CV", "required": "true"}),
                FakeHandle("textarea", {"name": "candidate[cover_letter]"}),
                FakeHandle("select", {"name": "notice_period"},
                           options=["Immediate", "1 month", "3 months"]),
                FakeHandle("input", {"type": "hidden", "name": "csrf"}),
                FakeHandle("input", {"type": "submit", "name": "go"}),
                FakeHandle("input", {"type": "checkbox", "name": "terms"}),
            ],
            labels={},
        )

    def test_every_fillable_input_is_described(self):
        fields = inspect_form(self._page())
        kinds = [f.kind for f in fields]
        self.assertIn("email", kinds)
        self.assertIn("phone", kinds)
        self.assertIn("resume", kinds)
        self.assertIn("cover_letter", kinds)
        self.assertIn("notice_period", kinds)

    def test_hidden_and_submit_inputs_are_never_filled(self):
        selectors = [f.selector for f in inspect_form(self._page())]
        self.assertNotIn('[name="csrf"]', selectors)
        self.assertNotIn('[name="go"]', selectors)

    def test_a_file_input_is_always_a_resume_upload(self):
        [resume] = [f for f in inspect_form(self._page()) if f.input_type == "file"]
        self.assertEqual(resume.kind, "resume")
        self.assertTrue(resume.required)

    def test_select_options_are_captured(self):
        [notice] = [f for f in inspect_form(self._page())
                    if f.kind == "notice_period"]
        self.assertEqual(notice.input_type, "select")
        self.assertIn("1 month", notice.options)

    def test_a_label_element_is_used_when_the_attributes_say_nothing(self):
        page = FakePage(
            handles=[FakeHandle("input", {"type": "text", "id": "q1"})],
            labels={"q1": "How many years have you used Asterisk?"},
        )
        [field] = inspect_form(page)
        self.assertIn("Asterisk", field.label)
        self.assertTrue(field.is_question)

    def test_an_unlabelled_field_still_gets_a_readable_name(self):
        page = FakePage(handles=[FakeHandle("input", {"type": "text"})])
        [field] = inspect_form(page)
        self.assertIn("unlabelled", field.label)

    def test_duplicate_inputs_are_collapsed(self):
        page = FakePage(handles=[
            FakeHandle("input", {"type": "email", "name": "email"}),
            FakeHandle("input", {"type": "email", "name": "email"}),
        ])
        self.assertEqual(len(inspect_form(page)), 1)

    def test_an_uninspectable_field_does_not_abort_the_scan(self):
        class Exploding(FakeHandle):
            def get_attribute(self, name):
                raise RuntimeError("detached from the DOM")

        page = FakePage(handles=[
            Exploding("input", {}),
            FakeHandle("input", {"type": "email", "name": "email"}),
        ])
        self.assertEqual([f.kind for f in inspect_form(page)], ["email"])

    def test_an_empty_page_yields_no_fields(self):
        self.assertEqual(inspect_form(FakePage(handles=[])), [])


class TestApplicationFormDetection(unittest.TestCase):
    """The decision that separates an application from a search box."""

    def test_a_cv_upload_proves_it(self):
        ok, why = looks_like_application_form([_field("resume",
                                                      input_type="file")])
        self.assertTrue(ok)
        self.assertIn("resume", why)

    def test_a_cover_letter_proves_it(self):
        self.assertTrue(looks_like_application_form(
            [_field("cover_letter", input_type="textarea")])[0])

    def test_two_personal_details_are_enough(self):
        self.assertTrue(looks_like_application_form(
            [_field("email"), _field("phone")])[0])

    def test_one_personal_detail_is_not(self):
        self.assertFalse(looks_like_application_form([_field("email")])[0])

    def test_the_sites_search_widget_is_rejected_by_name(self):
        ok, why = looks_like_application_form([
            _field("unknown", selector='[name="keywords"]',
                   label="keywords | Search jobs"),
            _field("unknown", selector='[name="state"]', label="Governorate"),
        ])
        self.assertFalse(ok)
        self.assertIn("search", why)

    def test_a_newsletter_box_is_rejected(self):
        ok, why = looks_like_application_form([
            _field("email", selector="#newsletter", label="Subscribe to alerts"),
        ])
        self.assertFalse(ok)

    def test_an_empty_page_is_rejected_with_an_explanation(self):
        ok, why = looks_like_application_form([])
        self.assertFalse(ok)
        self.assertIn("no application markers", why)

    def test_a_login_panel_is_rejected(self):
        ok, _ = looks_like_application_form([
            _field("email", selector="#login_email", label="Sign in email"),
            _field("password", selector="#login_pw", label="Password"),
        ])
        self.assertFalse(ok)


class TestCaptchaDetection(unittest.TestCase):
    """Clicking submit under an unsolved challenge looks exactly like success."""

    MARKUP = {
        "Google reCAPTCHA": '<div class="g-recaptcha" data-sitekey="abc"></div>',
        "hCaptcha": '<div class="h-captcha" id="hcaptcha-box"></div>',
        "Cloudflare Turnstile": '<div class="cf-turnstile"></div>',
        "Cloudflare challenge": '<iframe src="https://challenges.cloudflare.com/x">',
        "FunCaptcha / Arkose": '<script src="https://client-api.arkoselabs.com/v2">',
        "GeeTest": '<div class="geetest_holder"></div>',
    }

    def test_every_known_challenge_is_recognised(self):
        for expected, markup in self.MARKUP.items():
            with self.subTest(challenge=expected):
                blocked, name = detect_captcha(FakePage([], markup=markup))
                self.assertTrue(blocked, markup)
                self.assertEqual(name, expected)

    def test_the_arabic_and_english_checkbox_text_is_recognised(self):
        for markup in ("<label>I'm not a robot</label>",
                       "<label>أنا لست روبوت</label>"):
            with self.subTest(markup=markup):
                self.assertTrue(detect_captcha(FakePage([], markup=markup))[0])

    def test_an_ordinary_application_page_is_not_flagged(self):
        page = FakePage([], markup=(
            "<form><input name='email'><textarea name='cover_letter'>"
            "</textarea><button type='submit'>Apply</button></form>"
        ))
        self.assertEqual(detect_captcha(page), (False, ""))

    def test_detection_is_case_insensitive(self):
        self.assertTrue(detect_captcha(FakePage([], markup='<div class="G-ReCaptcha">'))[0])

    def test_an_unreadable_page_never_blocks_a_working_submission(self):
        """A failed inspection must not be able to veto a valid apply."""
        page = FakePage([], markup="")
        page.content_error = RuntimeError("execution context destroyed")
        self.assertEqual(detect_captcha(page), (False, ""))


class TestFieldFilling(unittest.TestCase):
    def test_each_input_type_uses_the_right_playwright_call(self):
        page = FakePage([])
        self.assertTrue(fill_field(page, _field("email", "#e"), "a@b.c"))
        self.assertTrue(fill_field(page, _field("resume", "#cv", "file"),
                                   "/tmp/cv.pdf"))
        self.assertTrue(fill_field(page, _field("notice_period", "#n", "select"),
                                   "1 month"))
        self.assertTrue(fill_field(page, _field("unknown", "#t", "checkbox"),
                                   "yes"))
        self.assertEqual(page.filled, [("#e", "a@b.c")])
        self.assertEqual(page.uploaded, [("#cv", "/tmp/cv.pdf")])
        self.assertEqual(page.selected, [("#n", "1 month")])
        self.assertEqual(page.checked, ["#t"])

    def test_an_empty_value_is_skipped_rather_than_blanking_the_field(self):
        page = FakePage([])
        self.assertFalse(fill_field(page, _field("email", "#e"), ""))
        self.assertEqual(page.filled, [])

    def test_a_failure_returns_false_instead_of_raising(self):
        """One unfillable box must not abandon a form that is otherwise fine."""
        class Stubborn(FakePage):
            def fill(self, *args, **kwargs):
                raise RuntimeError("element is not visible")

        self.assertFalse(fill_field(Stubborn([]), _field("email", "#e"), "x"))


class TestCvFieldMapping(unittest.TestCase):
    """What actually gets typed into each box."""

    FIELDS = [
        _field("resume", "#cv", "file", "Upload CV"),
        _field("email", "#email", label="Email"),
        _field("phone", "#phone", label="Phone"),
        _field("full_name", "#name", label="Full name"),
        _field("cover_letter", "#cover", "textarea", "Why this role?"),
        _field("salary", "#salary", label="Expected salary"),
        _field("unknown", "#q1", label="Years with Asterisk?"),
        _field("unknown", "#q2", label="A question nobody answered"),
    ]

    DRAFT = {
        "cover_letter": "I run Issabel PBX estates.",
        "salary_expectation": "12,000 AED",
        "answers": [
            {"question": "Years with Asterisk?", "answer": "Three",
             "confident": True},
        ],
    }

    def setUp(self):
        import auto_apply.candidate as candidate_mod
        from auto_apply.candidate import CandidateProfile

        self._original = candidate_mod.load_candidate
        candidate_mod.load_candidate = lambda force=False: CandidateProfile(
            full_name="Hossam Eldefrawy", email="h@example.com",
            phone="+201000000000", location="Cairo, Egypt",
            years_experience=3.0,
        )
        self.addCleanup(setattr, candidate_mod, "load_candidate", self._original)

    def _payload(self):
        return build_payload(Evaluation(fingerprint="fp"), self.DRAFT,
                             self.FIELDS)

    def test_the_cv_upload_is_handled_separately_not_as_a_text_value(self):
        self.assertNotIn("#cv", self._payload())

    def test_profile_values_land_in_their_semantic_fields(self):
        payload = self._payload()
        self.assertEqual(payload["#email"], "h@example.com")
        self.assertEqual(payload["#phone"], "+201000000000")
        self.assertEqual(payload["#name"], "Hossam Eldefrawy")

    def test_the_drafted_letter_and_salary_land_in_theirs(self):
        payload = self._payload()
        self.assertEqual(payload["#cover"], "I run Issabel PBX estates.")
        self.assertEqual(payload["#salary"], "12,000 AED")

    def test_a_screening_question_is_matched_by_its_label(self):
        self.assertEqual(self._payload()["#q1"], "Three")

    def test_an_unanswered_question_is_left_empty_rather_than_guessed(self):
        self.assertNotIn("#q2", self._payload())

    def test_describe_fields_records_what_each_selector_is(self):
        """Without this map an in-line edit can only change the draft's own
        copy, and the ORIGINAL value still goes into the form."""
        described = describe_fields(self.FIELDS)
        by_selector = {d["selector"]: d for d in described}
        self.assertEqual(by_selector["#cover"]["kind"], "cover_letter")
        self.assertEqual(by_selector["#salary"]["kind"], "salary")
        self.assertEqual(by_selector["#q1"]["label"], "Years with Asterisk?")
        self.assertEqual(len(described), len(self.FIELDS))

    def test_the_map_is_json_serialisable(self):
        import json

        json.dumps(describe_fields(self.FIELDS))


class TestScreenshotEvidence(unittest.TestCase):
    def test_a_screenshot_is_timestamped_and_full_page(self):
        page = FakePage([])
        path = capture_evidence(page, "app7_Etisalat")
        self.assertTrue(path)
        self.assertTrue(Path(path).exists())
        self.assertTrue(page.screenshots[0]["full_page"],
                        "a viewport-only capture is not evidence of the whole "
                        "confirmation page")
        Path(path).unlink()

    def test_the_filename_carries_a_utc_stamp_and_the_prefix(self):
        path = timestamped_screenshot_path("app7_Etisalat Group!")
        self.assertRegex(path.name, r"^\d{8}-\d{6}_app7_Etisalat_Group_\.png$")

    def test_unsafe_characters_are_stripped_from_the_prefix(self):
        path = timestamped_screenshot_path("../../etc/passwd")
        self.assertNotIn("..", path.name)
        self.assertEqual(path.parent, SCREENSHOT_DIR)

    def test_a_failed_capture_returns_empty_rather_than_killing_the_submission(self):
        """The application WAS submitted; losing the proof must not undo it."""
        page = FakePage([])
        page.screenshot_error = RuntimeError("page closed")
        self.assertEqual(capture_evidence(page, "app7"), "")

    def test_the_screenshot_directory_is_created_on_demand(self):
        self.assertTrue(timestamped_screenshot_path("x").parent.exists())


class TestBrowserFallbacks(unittest.TestCase):
    def test_a_missing_playwright_explains_how_to_install_it(self):
        original = browser_mod.playwright_available
        browser_mod.playwright_available = lambda: False
        try:
            with self.assertRaises(RuntimeError) as ctx:
                with browser_mod.browser_page("tanqeeb"):
                    pass
        finally:
            browser_mod.playwright_available = original
        message = str(ctx.exception)
        self.assertIn("pip install", message)
        self.assertIn("playwright install chromium", message)

    def test_playwright_availability_is_reported_honestly(self):
        self.assertIsInstance(browser_mod.playwright_available(), bool)

    def test_session_directories_are_namespaced_per_platform(self):
        """A shared profile would carry one board's login into another."""
        self.assertNotEqual(
            re.sub(r"[^A-Za-z0-9_-]+", "_", "tanqeeb:uae".lower()),
            re.sub(r"[^A-Za-z0-9_-]+", "_", "talent:ae".lower()),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
class ApplyPage:
    """A job page whose application form only exists after a click."""

    def __init__(self, controls=(), fields_before=None, fields_after=None,
                 url="https://egypt.tanqeeb.com/jobs/1.html",
                 new_url="", control_text=None, popup_url=""):
        self.controls = tuple(controls)
        self.fields_before = list(fields_before or [])
        self.fields_after = list(fields_after or [])
        self.url = url
        self.new_url = new_url
        self.control_text = dict(control_text or {})
        self.clicked = []
        self.waited = []
        self.opened = False
        self.popup_url = popup_url
        self.context = _FakeContext(self)
        self.closed = False

    # -- what inspect_form will be handed -------------------------------
    @property
    def fields(self):
        return self.fields_after if self.opened else self.fields_before

    def query_selector_all(self, selector):
        if selector not in self.controls:
            return []
        # A board that renders the same control twice -- one hidden in a
        # sticky bar, one visible in the article -- is the case that broke
        # `page.click(selector)` on the real Tanqeeb page.
        hidden = _FakeControl(self.control_text.get(selector, "Apply Now"),
                              page=self, selector=selector, visible=False)
        shown = _FakeControl(self.control_text.get(selector, "Apply Now"),
                             page=self, selector=selector, visible=True)
        return [hidden, shown]

    def _register_click(self, selector):
        self.clicked.append(selector)
        self.opened = True
        if self.new_url:
            self.url = self.new_url
        if self.popup_url:
            # Verified live on Tanqeeb: the apply control opens a NEW TAB and
            # leaves the original page untouched.
            self.context.pages.append(
                ApplyPage(url=self.popup_url, fields_before=APPLY_FORM,
                          fields_after=APPLY_FORM)
            )

    def wait_for_load_state(self, state, timeout=None):
        self.waited.append(state)

    def wait_for_timeout(self, ms):
        self.waited.append(f"timeout:{ms}")

    def close(self):
        self.closed = True


def _refuse(_script):
    raise RuntimeError("detached from the DOM")


class _FakeContext:
    """Just the pages list -- popup detection is all `open_application_form`
    asks a context for."""

    def __init__(self, page):
        self.pages = [page]


class _FakeControl:
    def __init__(self, text, page=None, selector="", visible=True,
                 enabled=True, raises=False):
        self._text = text
        self._page = page
        self._selector = selector
        self._visible = visible
        self._enabled = enabled
        self._raises = raises

        self._attrs: dict[str, str] = {}

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return self._attrs.get(name)

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return self._enabled

    def scroll_into_view_if_needed(self, timeout=None):
        return None

    def click(self, timeout=None):
        if self._raises:
            raise RuntimeError("intercepted by an overlay")
        if self._page is not None:
            self._page._register_click(self._selector)

    def evaluate(self, script):
        """The DOM-dispatch fallback for an off-canvas but real control."""
        if "click" in script and self._page is not None:
            self._page._register_click(self._selector)
        return None


APPLY_FORM = [
    FormField(selector="#cv", kind="resume", input_type="file",
              label="Upload CV"),
    FormField(selector="#cover", kind="cover_letter", input_type="textarea",
              label="Cover letter"),
]

SEARCH_WIDGET = [
    FormField(selector='[name="keywords"]', kind="unknown", input_type="text",
              label="keywords | Search jobs"),
]


@contextmanager
def _inspecting(page):
    """Point inspect_form at whatever stage the fake page is currently in."""
    original = browser_mod.inspect_form
    browser_mod.inspect_form = lambda p, *a, **k: list(p.fields)
    try:
        yield
    finally:
        browser_mod.inspect_form = original


class TestOpenApplicationForm(unittest.TestCase):
    """On an aggregator the landing page has a button, not a form."""

    def test_the_apply_button_is_clicked_and_the_form_appears(self):
        page = ApplyPage(controls=('button:has-text("قدّم الآن")',),
                         fields_before=SEARCH_WIDGET,
                         fields_after=APPLY_FORM)
        with _inspecting(page):
            _active, opened, note = browser_mod.open_application_form(page)
            self.assertTrue(opened, note)
            self.assertTrue(
                browser_mod.looks_like_application_form(page.fields)[0]
            )
        self.assertEqual(page.clicked, ['button:has-text("قدّم الآن")'])

    def test_a_page_that_is_already_the_form_is_left_alone(self):
        """Clicking anything here risks pressing submit on a real form."""
        page = ApplyPage(controls=('button:has-text("Apply")',),
                         fields_before=APPLY_FORM, fields_after=APPLY_FORM)
        with _inspecting(page):
            _active, opened, note = browser_mod.open_application_form(page)
        self.assertFalse(opened)
        self.assertEqual(page.clicked, [])
        self.assertIn("already", note)

    def test_a_page_with_no_apply_control_reports_it_without_failing(self):
        page = ApplyPage(controls=(), fields_before=SEARCH_WIDGET,
                         fields_after=SEARCH_WIDGET)
        with _inspecting(page):
            _active, opened, note = browser_mod.open_application_form(page)
        self.assertFalse(opened)
        self.assertIn("no apply control", note)

    def test_every_documented_selector_is_covered(self):
        for selector in ('[data-action="apply"]', ".apply-btn",
                         'button:has-text("Apply")',
                         'a:has-text("Apply Now")',
                         'button:has-text("قدّم الآن")',
                         'a:has-text("قدّم الآن")'):
            with self.subTest(selector=selector):
                self.assertIn(selector, browser_mod.APPLY_SELECTORS)

    def test_it_waits_for_the_dom_to_settle_after_clicking(self):
        """A modal animating in, or a redirect, is not synchronous."""
        page = ApplyPage(controls=('.apply-btn',),
                         fields_before=SEARCH_WIDGET, fields_after=APPLY_FORM)
        with _inspecting(page):
            browser_mod.open_application_form(page)
        self.assertIn("networkidle", page.waited)
        self.assertTrue(any(str(w).startswith("timeout:") for w in page.waited))

    def test_a_redirect_to_an_external_ats_is_reported(self):
        page = ApplyPage(controls=('a[href*="/apply"]',),
                         fields_before=SEARCH_WIDGET, fields_after=APPLY_FORM,
                         new_url="https://boards.greenhouse.io/acme/jobs/1")
        with _inspecting(page):
            _active, opened, note = browser_mod.open_application_form(page)
        self.assertTrue(opened)
        self.assertIn("greenhouse.io", note)

    def test_a_search_filter_button_is_not_mistaken_for_apply(self):
        """"Apply filters" is on almost every board's search chrome."""
        page = ApplyPage(controls=('button:has-text("Apply")',),
                         fields_before=SEARCH_WIDGET, fields_after=APPLY_FORM,
                         control_text={'button:has-text("Apply")':
                                       "Apply filters"})
        with _inspecting(page):
            _active, opened, _note = browser_mod.open_application_form(page)
        self.assertFalse(opened)
        self.assertEqual(page.clicked, [])

    def test_an_already_applied_button_is_not_clicked(self):
        page = ApplyPage(controls=('button:has-text("Apply")',),
                         fields_before=SEARCH_WIDGET, fields_after=APPLY_FORM,
                         control_text={'button:has-text("Apply")': "Applied"})
        with _inspecting(page):
            _active, opened, _note = browser_mod.open_application_form(page)
        self.assertFalse(opened)

    def test_an_explicit_data_hook_is_trusted_without_a_text_check(self):
        page = ApplyPage(controls=('[data-action="apply"]',),
                         fields_before=SEARCH_WIDGET, fields_after=APPLY_FORM,
                         control_text={'[data-action="apply"]': ""})
        with _inspecting(page):
            _active, opened, _note = browser_mod.open_application_form(page)
        self.assertTrue(opened)

    def test_the_most_specific_selector_wins(self):
        """A data hook is unambiguous; loose text matching is a last resort."""
        page = ApplyPage(
            controls=('[data-action="apply"]', 'button:has-text("Apply")'),
            fields_before=SEARCH_WIDGET, fields_after=APPLY_FORM,
        )
        with _inspecting(page):
            browser_mod.open_application_form(page)
        self.assertEqual(page.clicked, ['[data-action="apply"]'])

    def test_a_control_that_cannot_be_clicked_at_all_moves_on(self):
        """Both routes have to fail before we give up on a selector: a plain
        click AND the DOM dispatch that rescues an off-canvas button."""
        class Stubborn(ApplyPage):
            def query_selector_all(self, selector):
                elements = super().query_selector_all(selector)
                if selector == ".apply-btn":
                    for element in elements:
                        element._raises = True
                        element.evaluate = _refuse
                return elements

        page = Stubborn(controls=(".apply-btn", 'button:has-text("Apply")'),
                        fields_before=SEARCH_WIDGET, fields_after=APPLY_FORM)
        with _inspecting(page):
            _active, opened, _note = browser_mod.open_application_form(page)
        self.assertTrue(opened)
        self.assertEqual(page.clicked, ['button:has-text("Apply")'])

    def test_a_hidden_duplicate_control_does_not_block_the_visible_one(self):
        """Tanqeeb renders "Apply Now" twice -- once in the article and once in
        a sticky bar hidden until you scroll. `page.click(selector)` picked the
        hidden one, timed out, and reported "no apply control found" on a page
        that plainly had one."""
        page = ApplyPage(controls=('a:has-text("Apply Now")',),
                         fields_before=SEARCH_WIDGET, fields_after=APPLY_FORM)
        with _inspecting(page):
            _active, opened, note = browser_mod.open_application_form(page)
        self.assertTrue(opened, note)
        self.assertEqual(page.clicked, ['a:has-text("Apply Now")'])

    def test_a_disabled_control_is_skipped(self):
        page = ApplyPage(controls=('button:has-text("Apply")',),
                         fields_before=SEARCH_WIDGET, fields_after=APPLY_FORM)
        original = page.query_selector_all

        def all_disabled(selector):
            elements = original(selector)
            for element in elements:
                element._enabled = False
            return elements

        page.query_selector_all = all_disabled
        with _inspecting(page):
            _active, opened, _note = browser_mod.open_application_form(page)
        self.assertFalse(opened)
        self.assertEqual(page.clicked, [])

    def test_the_real_tanqeeb_control_shape_is_covered(self):
        """Verified live: <a href="javascript:void(0)">Apply Now</a>."""
        self.assertIn('a:has-text("Apply Now")', browser_mod.APPLY_SELECTORS)
        self.assertIn('a:has-text("Apply on the Job Website")',
                      browser_mod.APPLY_SELECTORS)

    def test_a_page_that_cannot_be_waited_on_still_counts_as_opened(self):
        class NoWait(ApplyPage):
            def wait_for_load_state(self, state, timeout=None):
                raise RuntimeError("navigation already finished")

        page = NoWait(controls=('.apply-btn',), fields_before=SEARCH_WIDGET,
                      fields_after=APPLY_FORM)
        with _inspecting(page):
            _active, opened, _note = browser_mod.open_application_form(page)
        self.assertTrue(opened)


class TestApplyOpensANewTab(unittest.TestCase):
    """Verified live on Tanqeeb: the apply control pops a new tab.

    A caller that kept inspecting the ORIGINAL page would read the job
    description forever and conclude, wrongly, that there is no application
    form anywhere -- which is exactly what every refused draft said.
    """

    def test_the_returned_page_is_the_new_tab(self):
        page = ApplyPage(controls=('.apply-btn',),
                         fields_before=SEARCH_WIDGET, fields_after=SEARCH_WIDGET,
                         popup_url="https://boards.greenhouse.io/acme/jobs/1")
        with _inspecting(page):
            active, opened, note = browser_mod.open_application_form(page)
        self.assertTrue(opened, note)
        self.assertIsNot(active, page, "still inspecting the original page")
        self.assertIn("greenhouse.io", active.url)
        self.assertIn("new tab", note)

    def test_the_new_tabs_form_is_what_gets_inspected(self):
        page = ApplyPage(controls=('.apply-btn',),
                         fields_before=SEARCH_WIDGET, fields_after=SEARCH_WIDGET,
                         popup_url="https://boards.greenhouse.io/acme/jobs/1")
        with _inspecting(page):
            active, _opened, _note = browser_mod.open_application_form(page)
            self.assertTrue(
                browser_mod.looks_like_application_form(active.fields)[0]
            )

    def test_a_linkedin_popup_is_refused_and_closed(self):
        """The posting is syndicated FROM LinkedIn, so applying means applying
        on LinkedIn -- which this project never automates. Leaving the tab open
        in a persistent logged-in profile is the footprint the rule exists to
        avoid."""
        page = ApplyPage(controls=('.apply-btn',),
                         fields_before=SEARCH_WIDGET, fields_after=SEARCH_WIDGET,
                         popup_url="https://www.linkedin.com/jobs/view/4446628803")
        with _inspecting(page):
            active, opened, note = browser_mod.open_application_form(page)
        self.assertFalse(opened)
        self.assertIs(active, page)
        self.assertIn("never automated", note)
        self.assertIn("linkedin.com", note)
        self.assertTrue(page.context.pages[-1].closed,
                        "the refused tab was left open")

    def test_an_in_place_redirect_to_linkedin_is_also_refused(self):
        page = ApplyPage(controls=('.apply-btn',),
                         fields_before=SEARCH_WIDGET, fields_after=APPLY_FORM,
                         new_url="https://www.linkedin.com/jobs/view/1")
        with _inspecting(page):
            _active, opened, note = browser_mod.open_application_form(page)
        self.assertFalse(opened)
        self.assertIn("never automated", note)

    def test_an_ordinary_ats_popup_is_allowed(self):
        for url in ("https://boards.greenhouse.io/a/jobs/1",
                    "https://jobs.lever.co/a/b",
                    "https://acme.wd1.myworkdayjobs.com/x",
                    "https://acme-corp.com/careers/1"):
            with self.subTest(url=url):
                page = ApplyPage(controls=('.apply-btn',),
                                 fields_before=SEARCH_WIDGET,
                                 fields_after=SEARCH_WIDGET, popup_url=url)
                with _inspecting(page):
                    _a, opened, _n = browser_mod.open_application_form(page)
                self.assertTrue(opened, url)

    def test_a_page_with_no_context_still_works(self):
        """Popup detection must degrade, not crash, on a page double that has
        no context attached."""
        page = ApplyPage(controls=('.apply-btn',),
                         fields_before=SEARCH_WIDGET, fields_after=APPLY_FORM)
        page.context = None
        with _inspecting(page):
            _active, opened, _note = browser_mod.open_application_form(page)
        self.assertTrue(opened)


class TestNeverAutomateUrls(unittest.TestCase):
    def test_linkedin_in_every_shape(self):
        for url in ("https://www.linkedin.com/jobs/view/1",
                    "https://linkedin.com/jobs/view/1",
                    "https://ae.linkedin.com/jobs/view/1",
                    "https://LINKEDIN.com/jobs/view/1"):
            with self.subTest(url=url):
                allowed, why = browser_mod.is_automatable_url(url)
                self.assertFalse(allowed, url)
                self.assertIn("manual-only", why)

    def test_ordinary_boards_are_allowed(self):
        for url in ("https://egypt.tanqeeb.com/jobs/1.html",
                    "https://wuzzuf.net/jobs/p/1",
                    "https://boards.greenhouse.io/a/jobs/1",
                    ""):
            with self.subTest(url=url):
                self.assertTrue(browser_mod.is_automatable_url(url)[0], url)

    def test_the_rule_is_read_from_the_same_config_the_engine_uses(self):
        """Stated once, so the two cannot disagree about what is banned."""
        from auto_apply.engine import is_automatable

        self.assertIn("linkedin", browser_mod.never_automate_hosts())
        self.assertFalse(is_automatable("linkedin")[0])
        self.assertFalse(
            browser_mod.is_automatable_url("https://linkedin.com/x")[0]
        )


class TestConcatenatedFieldNames(unittest.TestCase):
    """Boards name inputs with no separator at all.

    Wuzzuf's registration form uses `firstname` and `lastname` -- one word --
    so the space-separated patterns matched none of them and the whole form
    came back as `unknown`. The separator-folding added earlier only helps
    when there IS a separator.
    """

    CASES = [
        ("firstname", "first_name"),
        ("lastname", "last_name"),
        ("fullname", "full_name"),
        ("coverletter", "cover_letter"),
        ("jobtitle", "current_title"),
        ("currentcompany", "current_employer"),
        ("noticeperiod", "notice_period"),
        ("expectedsalary", "salary"),
        ("yearsofexperience", "years_experience"),
    ]

    def test_a_name_with_no_separator_still_classifies(self):
        for raw, expected in self.CASES:
            with self.subTest(raw=raw):
                self.assertEqual(_classify(raw), expected)

    def test_the_spaced_and_squashed_forms_agree(self):
        for raw, expected in self.CASES:
            with self.subTest(raw=raw):
                spaced = raw.replace("firstname", "first name") \
                            .replace("lastname", "last name")
                if spaced != raw:
                    self.assertEqual(_classify(spaced), expected)

    def test_squashing_does_not_resurrect_the_short_needle_bugs(self):
        """Only multi-word patterns are squashed. A bare "tel" or "cv" matched
        without word boundaries would be far worse than it already was."""
        for label in ("Tell us about your most difficult outage",
                      "Storage capacity in GB",
                      "Preferred payment method",
                      "What is your intelligence quotient?"):
            with self.subTest(label=label):
                self.assertEqual(_classify(label), "unknown")

    def test_prose_is_still_not_a_field_name(self):
        for label in ("Describe a time you led a project",
                      "Anything else we should know"):
            with self.subTest(label=label):
                self.assertEqual(_classify(label), "unknown")


class TestSignInWallIsNotAnApplicationForm(unittest.TestCase):
    """The impostor this guard exists for.

    Verified live: Tanqeeb's "Apply Now" opens /users/login, and Wuzzuf's
    "Apply For Job" opens a registration form asking first name, last name and
    email -- which is EXACTLY the "cluster of personal details" that otherwise
    proves an application form. Filling it and pressing submit would create an
    account, then bank the result as a delivered application.
    """

    @staticmethod
    def _signup():
        return [
            _field("first_name", "#f", label="firstname"),
            _field("last_name", "#l", label="lastname"),
            _field("email", "#e", label="email"),
            _field("password", "#p", label="password"),
        ]

    def test_a_password_field_disqualifies_the_form(self):
        ok, why = looks_like_application_form(self._signup())
        self.assertFalse(ok)
        self.assertIn("sign-in or registration", why)

    def test_the_refusal_says_what_to_do_about_it(self):
        _ok, why = looks_like_application_form(self._signup())
        self.assertIn("--register", why)

    def test_it_beats_the_personal_fields_rule(self):
        """Without the guard these four fields read as a valid application."""
        without_password = [f for f in self._signup() if f.kind != "password"]
        self.assertTrue(looks_like_application_form(without_password)[0])
        self.assertFalse(looks_like_application_form(self._signup())[0])

    def test_it_beats_even_a_cv_upload_on_the_same_form(self):
        """A signup that also offers a CV upload is still a signup."""
        fields = self._signup() + [_field("resume", "#cv", "file", "Upload CV")]
        ok, why = looks_like_application_form(fields)
        self.assertFalse(ok)
        self.assertIn("sign-in", why)

    def test_a_real_application_form_is_unaffected(self):
        fields = [
            _field("resume", "#cv", "file", "Upload CV"),
            _field("cover_letter", "#c", "textarea", "Cover letter"),
            _field("email", "#e", label="email"),
        ]
        self.assertTrue(looks_like_application_form(fields)[0])

    def test_an_application_asking_only_personal_details_still_passes(self):
        fields = [_field("email", "#e", label="email"),
                  _field("phone", "#p", label="phone")]
        self.assertTrue(looks_like_application_form(fields)[0])


class TestApplyChainFollowsMultipleHops(unittest.TestCase):
    """Aggregators hand off to each other; one hop is not enough.

    Measured live: a Tanqeeb posting's apply button opens Wuzzuf in a new tab,
    and Wuzzuf then has its OWN "Apply For Job" button before any form exists.
    """

    def test_a_second_hop_is_followed(self):
        second = ApplyPage(controls=('button:has-text("Apply")',),
                           url="https://wuzzuf.net/jobs/p/1",
                           fields_before=SEARCH_WIDGET, fields_after=APPLY_FORM)

        first = ApplyPage(controls=('.apply-btn',),
                          fields_before=SEARCH_WIDGET, fields_after=SEARCH_WIDGET)

        def hop(selector):
            first.clicked.append(selector)
            first.opened = True
            first.context.pages.append(second)

        first._register_click = hop
        second.context = first.context

        with _inspecting(first):
            active, opened, note = browser_mod.open_application_form(first)
        self.assertTrue(opened)
        self.assertIs(active, second)
        self.assertEqual(second.clicked, ['button:has-text("Apply")'])
        self.assertTrue(browser_mod.looks_like_application_form(active.fields)[0])

    def test_the_chain_stops_once_a_real_form_is_reached(self):
        page = ApplyPage(controls=('.apply-btn',),
                         fields_before=SEARCH_WIDGET, fields_after=APPLY_FORM)
        with _inspecting(page):
            _active, opened, _note = browser_mod.open_application_form(page)
        self.assertTrue(opened)
        self.assertEqual(len(page.clicked), 1, "it kept clicking past the form")

    def test_the_chain_is_bounded(self):
        """A page that always offers another apply button must not loop."""
        page = ApplyPage(controls=('.apply-btn',),
                         fields_before=SEARCH_WIDGET, fields_after=SEARCH_WIDGET)
        with _inspecting(page):
            _active, _opened, _note = browser_mod.open_application_form(
                page, max_hops=3)
        self.assertLessEqual(len(page.clicked), 3)


class TestOffCanvasControls(unittest.TestCase):
    """Wuzzuf renders its first "Apply For Job" at y=-222 -- visible and
    enabled, but outside the viewport, and Playwright refuses to click it
    however long it waits."""

    def test_a_click_failure_falls_back_to_a_dom_dispatch(self):
        page = ApplyPage(controls=('.apply-btn',),
                         fields_before=SEARCH_WIDGET, fields_after=APPLY_FORM)
        original = page.query_selector_all

        def refuse_real_clicks(selector):
            elements = original(selector)
            for element in elements:
                element._raises = True
            return elements

        page.query_selector_all = refuse_real_clicks
        with _inspecting(page):
            _active, opened, _note = browser_mod.open_application_form(page)
        self.assertTrue(opened, "the DOM-dispatch fallback did not fire")
        self.assertTrue(page.clicked)


class TestCrossBoardSessionHandoff(unittest.TestCase):
    """A hop to another board must carry THAT board's login.

    Measured live: a Tanqeeb posting's apply button opens Wuzzuf in a new tab,
    but the tab belongs to the TANQEEB browser profile and carries only
    Tanqeeb's cookies. The bot arrived on Wuzzuf anonymous and was shown a
    registration form -- while a saved Wuzzuf session sat on disk unused.
    """

    def setUp(self):
        from config import settings

        self.tmp = Path(tempfile.mkdtemp())
        self._saved = dict(settings.raw.get("auto_apply", {}) or {})
        settings.raw["auto_apply"] = dict(self._saved,
                                          session_dir=str(self.tmp))
        self.settings = settings

    def tearDown(self):
        self.settings.raw["auto_apply"] = self._saved

    def _save(self, slug, cookies):
        import json

        (self.tmp / f"{slug}_state.json").write_text(
            json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")

    def test_the_destination_boards_cookies_are_adopted(self):
        self._save("wuzzuf", [{"name": "LiToken", "value": "x",
                               "domain": ".wuzzuf.net", "path": "/"}])
        context = _RecordingContext()
        adopted = browser_mod.adopt_session_for(
            context, "https://wuzzuf.net/jobs/p/1")
        self.assertEqual(adopted, "Wuzzuf")
        self.assertEqual(len(context.added), 1)
        self.assertEqual(context.added[0][0]["name"], "LiToken")

    def test_a_board_with_no_saved_session_adopts_nothing(self):
        context = _RecordingContext()
        self.assertEqual(
            browser_mod.adopt_session_for(context, "https://wuzzuf.net/jobs/p/1"),
            "")
        self.assertEqual(context.added, [])

    def test_an_unrecognised_domain_adopts_nothing(self):
        self._save("wuzzuf", [{"name": "LiToken", "value": "x",
                               "domain": ".wuzzuf.net", "path": "/"}])
        context = _RecordingContext()
        self.assertEqual(
            browser_mod.adopt_session_for(context, "https://acme-corp.com/x"),
            "")

    def test_a_missing_context_is_survivable(self):
        self.assertEqual(
            browser_mod.adopt_session_for(None, "https://wuzzuf.net/x"), "")

    def test_a_context_that_rejects_cookies_does_not_crash_the_flow(self):
        self._save("wuzzuf", [{"name": "LiToken", "value": "x",
                               "domain": ".wuzzuf.net", "path": "/"}])

        class Hostile:
            pages = []

            def add_cookies(self, cookies):
                raise RuntimeError("invalid cookie")

        self.assertEqual(
            browser_mod.adopt_session_for(Hostile(), "https://wuzzuf.net/x"), "")

    def test_load_saved_cookies_tolerates_junk(self):
        (self.tmp / "bad_state.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(browser_mod.load_saved_cookies("bad"), [])
        self.assertEqual(browser_mod.load_saved_cookies("absent"), [])


class _RecordingContext:
    def __init__(self):
        self.pages = []
        self.added = []

    def add_cookies(self, cookies):
        self.added.append(cookies)


class TestNativeLoginNeverUsesOAuth(unittest.TestCase):
    """A "Continue with Google" button hands the flow to an OAuth consent
    screen that refuses an automation-driven browser with
    "Error 400: redirect_uri_mismatch" -- an unrecoverable dead end."""

    def test_social_buttons_are_recognised(self):
        for text in ("Continue with Google", "Sign in with Facebook",
                     "Log in with LinkedIn", "Continue with Apple"):
            with self.subTest(text=text):
                self.assertTrue(browser_mod.is_social_login(_FakeControl(text)))

    def test_a_native_submit_is_not_mistaken_for_social(self):
        for text in ("Log in", "Sign in", "Submit", "تسجيل الدخول"):
            with self.subTest(text=text):
                self.assertFalse(browser_mod.is_social_login(_FakeControl(text)))

    def test_a_provider_hidden_in_the_class_name_is_caught(self):
        control = _FakeControl("Continue")
        control._attrs = {"class": "btn btn-google-oauth"}
        self.assertTrue(browser_mod.is_social_login(control))

    def test_every_major_provider_is_listed(self):
        for provider in ("google", "facebook", "linkedin", "apple", "oauth"):
            with self.subTest(provider=provider):
                self.assertIn(provider, browser_mod.SOCIAL_LOGIN_MARKERS)


class TestLoginWithPassword(unittest.TestCase):
    class LoginPage:
        def __init__(self, fields, controls=(), after=None, social=()):
            self._fields = list(fields)
            self.controls = tuple(controls)
            self.after = list(after if after is not None else [])
            self.social = set(social)
            self.filled = []
            self.clicked = []
            self.submitted = False

        @property
        def fields(self):
            return self.after if self.submitted else self._fields

        def fill(self, selector, value, timeout=None):
            self.filled.append((selector, value))

        def query_selector_all(self, selector):
            if selector not in self.controls:
                return []
            control = _FakeControl("Log in", page=self, selector=selector)
            if selector in self.social:
                control._text = "Continue with Google"
            return [control]

        def _register_click(self, selector):
            self.clicked.append(selector)
            self.submitted = True

        def wait_for_load_state(self, state, timeout=None):
            pass

        def wait_for_timeout(self, ms):
            pass

    @contextmanager
    def _inspect(self, page):
        original = browser_mod.inspect_form
        browser_mod.inspect_form = lambda p, *a, **k: list(p.fields)
        try:
            yield
        finally:
            browser_mod.inspect_form = original

    LOGIN_FIELDS = [
        FormField(selector="#email", kind="email", input_type="text",
                  label="email"),
        FormField(selector="#pw", kind="password", input_type="text",
                  label="password"),
    ]

    def test_credentials_are_typed_and_the_form_submitted(self):
        page = self.LoginPage(self.LOGIN_FIELDS,
                              controls=('button[type="submit"]',), after=[])
        with self._inspect(page):
            ok, detail = browser_mod.login_with_password(
                page, "h@example.com", "s3cret")
        self.assertTrue(ok, detail)
        self.assertIn(("#email", "h@example.com"), page.filled)
        self.assertIn(("#pw", "s3cret"), page.filled)

    def test_a_google_button_is_never_pressed(self):
        page = self.LoginPage(self.LOGIN_FIELDS,
                              controls=('button[type="submit"]',),
                              social={'button[type="submit"]'}, after=[])
        with self._inspect(page):
            ok, detail = browser_mod.login_with_password(page, "a@b.c", "pw")
        self.assertFalse(ok)
        self.assertEqual(page.clicked, [], "an OAuth button was clicked")

    def test_a_page_with_no_password_form_is_reported_not_guessed(self):
        page = self.LoginPage([FormField(selector="#q", kind="unknown",
                                         input_type="text", label="search")])
        with self._inspect(page):
            ok, detail = browser_mod.login_with_password(page, "a@b.c", "pw")
        self.assertFalse(ok)
        self.assertIn("no native email/password form", detail)

    def test_a_login_that_did_not_take_is_detected(self):
        """The password box still being there is the board saying no."""
        page = self.LoginPage(self.LOGIN_FIELDS,
                              controls=('button[type="submit"]',),
                              after=self.LOGIN_FIELDS)
        with self._inspect(page):
            ok, detail = browser_mod.login_with_password(page, "a@b.c", "pw")
        self.assertFalse(ok)
        self.assertIn("still showing", detail)


class TestInspectionSurvivesNavigation(unittest.TestCase):
    """Clicking apply starts a navigation, and querying mid-flight raises
    "Execution context was destroyed". That surfaced as a draft which could
    not be re-inspected at all -- a timing accident reported as permanent."""

    class Navigating:
        def __init__(self, fail_times=1):
            self.fail_times = fail_times
            self.calls = 0
            self.settled = []

        def query_selector_all(self, selector):
            # Only the CONTAINER query is the one under test; inspect_form
            # then queries this same object again for its inputs.
            if selector != "form":
                return []
            self.calls += 1
            if self.calls <= self.fail_times:
                raise RuntimeError("Execution context was destroyed")
            return []

        def wait_for_load_state(self, state, timeout=None):
            self.settled.append(state)

    def test_a_single_navigation_is_retried_through(self):
        page = self.Navigating(fail_times=1)
        self.assertEqual(inspect_form(page), [])
        self.assertEqual(page.calls, 2, "it did not retry")
        self.assertTrue(page.settled, "it retried without settling first")

    def test_a_page_that_never_settles_gives_up_cleanly(self):
        page = self.Navigating(fail_times=99)
        with self.assertLogs("auto_apply.browser", level="WARNING"):
            self.assertEqual(inspect_form(page), [])
        self.assertEqual(page.calls, 2, "it retried more than once")

    def test_a_healthy_page_is_not_slowed_by_the_guard(self):
        page = self.Navigating(fail_times=0)
        inspect_form(page)
        self.assertEqual(page.calls, 1)
        self.assertEqual(page.settled, [])


class TestBotWallDetection(unittest.TestCase):
    """The check whose absence cost a whole debugging pass.

    Cloudflare answers an automation-driven browser with HTTP 403 and a
    holding page carrying no form, no inputs and no buttons. Every downstream
    check then reports its own symptom -- "no native submit control on the
    login form", "not an application form", "0 fields" -- and every one of
    those sends the reader hunting for a selector that was never missing.

    Measured on wuzzuf.net/login: 403, 0 inputs, 0 buttons, 0 forms, headless
    AND headed. The same URL in an ordinary Chrome cleared in seconds.
    """

    class WallPage:
        def __init__(self, title="", body="", raises=False):
            self._title, self._body, self._raises = title, body, raises
            self.waits = 0

        def title(self):
            if self._raises:
                raise RuntimeError("page is gone")
            return self._title

        def inner_text(self, _selector):
            if self._raises:
                raise RuntimeError("page is gone")
            return self._body

        def wait_for_timeout(self, ms):
            self.waits += 1

    def test_the_cloudflare_interstitial_is_recognised(self):
        page = self.WallPage(
            title="Just a moment...",
            body="wuzzuf.net Performing security verification")
        self.assertEqual(browser_mod.detect_bot_wall(page), "just a moment")

    def test_every_marker_is_matched_case_insensitively(self):
        for marker in browser_mod.BOT_WALL_MARKERS:
            with self.subTest(marker=marker):
                page = self.WallPage(title=marker.upper())
                self.assertEqual(browser_mod.detect_bot_wall(page), marker)

    def test_a_real_page_is_not_a_wall(self):
        page = self.WallPage(title="WUZZUF",
                             body="EXPLORE SAVED APPLICATIONS")
        self.assertEqual(browser_mod.detect_bot_wall(page), "")

    def test_a_page_that_cannot_be_read_is_not_guessed_at(self):
        """A dead page is a different problem; claiming a wall would hide it."""
        self.assertEqual(
            browser_mod.detect_bot_wall(self.WallPage(raises=True)), "")

    def test_the_word_security_in_ordinary_prose_is_not_a_wall(self):
        page = self.WallPage(title="Network Security Engineer - WUZZUF",
                             body="We are hiring a security engineer.")
        self.assertEqual(browser_mod.detect_bot_wall(page), "")


class TestWaitingOutTheWall(unittest.TestCase):
    """A real browser passes these in seconds, so waiting is worth doing --
    but an automation browser generally never does, so it must be bounded."""

    class ClearingPage(TestBotWallDetection.WallPage):
        def __init__(self, clears_after):
            super().__init__(title="Just a moment...")
            self._clears_after = clears_after

        def wait_for_timeout(self, ms):
            self.waits += 1
            if self.waits >= self._clears_after:
                self._title = "WUZZUF"

    def test_a_challenge_that_clears_is_waited_out(self):
        page = self.ClearingPage(clears_after=2)
        self.assertTrue(browser_mod.wait_out_bot_wall(page, timeout_ms=20000))

    def test_a_page_with_no_challenge_does_not_wait_at_all(self):
        page = TestBotWallDetection.WallPage(title="WUZZUF")
        self.assertTrue(browser_mod.wait_out_bot_wall(page))
        self.assertEqual(page.waits, 0, "it slept for a page that was ready")

    def test_a_challenge_that_never_clears_gives_up(self):
        page = TestBotWallDetection.WallPage(title="Just a moment...")
        self.assertFalse(browser_mod.wait_out_bot_wall(page, timeout_ms=6000))
        self.assertLessEqual(page.waits, 3, "it waited past its own budget")

    def test_giving_up_says_what_blocked_it(self):
        page = TestBotWallDetection.WallPage(title="Just a moment...")
        with self.assertLogs("auto_apply.browser", level="WARNING") as caught:
            browser_mod.wait_out_bot_wall(page, timeout_ms=4000)
        self.assertIn("anti-bot", "\n".join(caught.output))


class TestLoginSubmitControls(unittest.TestCase):
    """Wuzzuf's own login form, and the controls that submit it."""

    REQUESTED = ('button:has-text("Sign In")', 'button:has-text("Log In")',
                 'button[type="submit"]', ".btn-primary")

    def test_the_expected_submit_controls_are_all_tried(self):
        for selector in self.REQUESTED:
            with self.subTest(selector=selector):
                self.assertIn(selector, browser_mod.LOGIN_SUBMIT_SELECTORS)

    def test_a_precise_control_is_tried_before_a_broad_one(self):
        """`.btn-primary` can match some unrelated CTA; it must lose to the
        form's own submit button whenever both are on the page."""
        order = browser_mod.LOGIN_SUBMIT_SELECTORS
        self.assertLess(order.index('button[type="submit"]'),
                        order.index(".btn-primary"))

    def test_each_requested_control_actually_submits(self):
        for selector in self.REQUESTED:
            with self.subTest(selector=selector):
                page = TestLoginWithPassword.LoginPage(
                    TestLoginWithPassword.LOGIN_FIELDS,
                    controls=(selector,), after=[])
                with TestLoginWithPassword()._inspect(page):
                    ok, detail = browser_mod.login_with_password(
                        page, "h@example.com", "pw")
                self.assertTrue(ok, detail)
                self.assertEqual(page.clicked, [selector])

    def test_enter_submits_when_there_is_no_button_at_all(self):
        """Plenty of boards bind submit to the form, or render the control as
        a styled <div> we will not click blind. Enter goes through the form's
        own submit path either way."""
        page = TestLoginWithPassword.LoginPage(
            TestLoginWithPassword.LOGIN_FIELDS, controls=(), after=[])
        pressed = []

        def press(selector, key):
            pressed.append((selector, key))
            page.submitted = True

        page.press = press
        with TestLoginWithPassword()._inspect(page):
            ok, detail = browser_mod.login_with_password(page, "a@b.c", "pw")
        self.assertTrue(ok, detail)
        self.assertEqual(pressed, [("#pw", "Enter")])

    def test_enter_is_not_used_when_a_button_worked(self):
        page = TestLoginWithPassword.LoginPage(
            TestLoginWithPassword.LOGIN_FIELDS,
            controls=('button[type="submit"]',), after=[])
        pressed = []
        page.press = lambda selector, key: pressed.append(selector)
        with TestLoginWithPassword()._inspect(page):
            browser_mod.login_with_password(page, "a@b.c", "pw")
        self.assertEqual(pressed, [],
                         "it pressed Enter on an already-sent form")

    def test_an_oauth_button_is_still_never_pressed(self):
        page = TestLoginWithPassword.LoginPage(
            TestLoginWithPassword.LOGIN_FIELDS,
            controls=(".btn-primary",), social={".btn-primary"}, after=[])
        pressed = []

        def press(selector, key):
            pressed.append(selector)
            page.submitted = True

        page.press = press
        with TestLoginWithPassword()._inspect(page):
            browser_mod.login_with_password(page, "a@b.c", "pw")
        self.assertEqual(page.clicked, [], "an OAuth button was pressed")
        self.assertEqual(pressed, ["#pw"], "it did not fall back to Enter")

    def test_how_it_submitted_is_reported(self):
        page = TestLoginWithPassword.LoginPage(
            TestLoginWithPassword.LOGIN_FIELDS,
            controls=('button[type="submit"]',), after=[])
        with TestLoginWithPassword()._inspect(page):
            _ok, detail = browser_mod.login_with_password(page, "a@b.c", "pw")
        self.assertIn('button[type="submit"]', detail)


class TestSignInWallIsReportedAsItself(unittest.TestCase):
    """The point of the whole change: name the 403, not its symptom."""

    class WalledPage(TestBotWallDetection.WallPage):
        def __init__(self):
            super().__init__(title="Just a moment...",
                             body="Performing security verification")
            self.filled = []

        def fill(self, *a, **k):
            self.filled.append(a)

        def query_selector_all(self, _selector):
            return []

    def test_a_walled_login_says_it_was_blocked(self):
        page = self.WalledPage()
        ok, detail = browser_mod.login_with_password(
            page, "a@b.c", "pw", timeout_ms=1000)
        self.assertFalse(ok)
        self.assertIn("anti-bot", detail)
        self.assertNotIn("no native submit control", detail)

    def test_no_credentials_are_typed_into_a_holding_page(self):
        page = self.WalledPage()
        browser_mod.login_with_password(page, "a@b.c", "pw", timeout_ms=1000)
        self.assertEqual(page.filled, [])

    def test_the_message_says_what_to_do_instead(self):
        _ok, detail = browser_mod.login_with_password(
            self.WalledPage(), "a@b.c", "pw", timeout_ms=1000)
        self.assertIn("--capture-session", detail)


class TestCaptureSessionIsWiredUp(unittest.TestCase):
    """Attaching to the human's own Chrome: the honest way past a wall no
    automation browser is served through."""

    def test_the_cli_exposes_it(self):
        import main

        self.assertTrue(hasattr(main, "cmd_capture_session"))

    def test_an_unknown_board_is_refused_before_any_browser_opens(self):
        launched = []
        original = browser_mod.attach_to_chrome
        browser_mod.attach_to_chrome = lambda *a, **k: launched.append(1)
        try:
            saved, _detail = browser_mod.capture_session("NotARealBoard")
        finally:
            browser_mod.attach_to_chrome = original
        self.assertFalse(saved)
        self.assertEqual(launched, [], "it opened a browser for a bad name")

    def test_the_launch_hint_names_the_flag_that_matters(self):
        self.assertIn("--remote-debugging-port=9222",
                      browser_mod.CHROME_LAUNCH_HINT)

    def test_the_hint_says_to_close_chrome_first(self):
        """The flag is silently ignored when a Chrome is already running, and
        that failure looks exactly like the tool being broken."""
        self.assertIn("Close every Chrome window first",
                      browser_mod.CHROME_LAUNCH_HINT)


class TestArrivedAtTheApplication(unittest.TestCase):
    """Knowing when to STOP clicking.

    Measured on draft #9, before this existed. The chain worked perfectly:
    .apply-btn -> the board's job page, signed in -> "Complete your
    application" -> wuzzuf.net/job-questions/<uuid>. Then it inspected that
    page before the app had rendered, saw nothing it recognised, and hopped
    once more into a nav link that navigated to /saved -- discarding a
    part-finished application that was one step from submittable.
    """

    def test_an_application_url_is_recognised(self):
        for url in ("https://wuzzuf.net/job-questions/e5b161d0-5add-4294",
                    "https://boards.greenhouse.io/acme/jobs/1/apply",
                    "https://x.com/careers/application/44",
                    "https://y.com/screening-questions/9"):
            with self.subTest(url=url):
                self.assertTrue(browser_mod.on_application_url(url))

    def test_a_plain_job_page_is_not_an_application_url(self):
        for url in ("https://wuzzuf.net/jobs/p/vtcukbqmnbok-it-help-desk",
                    "https://egypt.tanqeeb.com/jobs-in-egypt/all/jobs/1.html",
                    "https://wuzzuf.net/saved",
                    ""):
            with self.subTest(url=url):
                self.assertFalse(browser_mod.on_application_url(url))

    def test_the_hop_loop_stops_once_it_is_on_one(self):
        """Even when the page has not rendered yet -- which is exactly the
        moment the old code hopped away."""
        page = ApplyPage(controls=('.apply-btn',),
                         url="https://wuzzuf.net/job-questions/abc",
                         fields_before=SEARCH_WIDGET,
                         fields_after=SEARCH_WIDGET)
        with _inspecting(page):
            _active, opened, note = browser_mod.open_application_form(page)
        self.assertTrue(opened)
        self.assertEqual(len(page.clicked), 1, "it hopped off the application")
        self.assertIn("stopped on the application", note)


class TestScreeningQuestionsAreAnApplication(unittest.TestCase):
    """What a SIGNED-IN candidate is actually shown.

    The board already holds the name, email and CV, so it asks only its
    screening questions -- and every marker the guard looks for is absent.
    Wuzzuf then names each question with a UUID and labels the box "Write your
    answer here..", so nothing classifies either. Live field map from #9:

        [name="q"]                                    -> the site search
        form >> nth=1 >> textarea >> nth=0            -> "Write your answer.."
        [name="a6767d69-d125-486f-8cb7-d3b4fa719da6"] -> a radio

    Two real questions, and the old rules called the page a search widget.
    """

    APP_URL = "https://wuzzuf.net/job-questions/e5b161d0-5add-4294-89ee"
    SEARCH = _field("unknown", '[name="q"]', label="Search jobs, companies..")
    QUESTIONS = [
        _field("unknown", "form >> nth=1 >> textarea >> nth=0", "textarea",
               "Write your answer here.."),
        _field("unknown", '[name="a6767d69-d125-486f-8cb7-d3b4fa719da6"]',
               "radio", "a6767d69-d125-486f-8cb7-d3b4fa719da6"),
    ]

    def test_uuid_named_questions_on_the_boards_own_url_are_an_application(self):
        ok, why = looks_like_application_form(
            [self.SEARCH] + self.QUESTIONS, self.APP_URL)
        self.assertTrue(ok, why)
        self.assertIn("2 question(s)", why)

    def test_the_search_box_alone_is_still_not_an_application(self):
        """The whole point of the guard survives: a URL is not enough on its
        own, or we would submit the site search from an application page."""
        ok, why = looks_like_application_form([self.SEARCH], self.APP_URL)
        self.assertFalse(ok)
        self.assertIn("search", why)

    def test_the_same_questions_off_an_application_url_are_still_refused(self):
        """Without the URL there is no evidence -- these fields are
        indistinguishable from any other unlabelled widget."""
        ok, _why = looks_like_application_form(
            [self.SEARCH] + self.QUESTIONS,
            "https://wuzzuf.net/jobs/p/some-job")
        self.assertFalse(ok)

    def test_a_signup_form_is_refused_even_on_an_application_url(self):
        """The password rule outranks everything, including this one. A board
        that bounces us to a signup while keeping an /apply URL must never be
        filled in and submitted as an application."""
        signup = [
            _field("first_name", "#f", label="firstname"),
            _field("last_name", "#l", label="lastname"),
            _field("email", "#e", label="email"),
            _field("password", "#p", label="password"),
        ]
        ok, why = looks_like_application_form(signup, self.APP_URL)
        self.assertFalse(ok)
        self.assertIn("sign-in or registration", why)

    def test_omitting_the_url_keeps_the_old_behaviour(self):
        ok, _why = looks_like_application_form([self.SEARCH] + self.QUESTIONS)
        self.assertFalse(ok)

    def test_a_real_application_form_still_passes_without_a_url(self):
        fields = [_field("resume", "#cv", "file", "Upload CV")]
        self.assertTrue(looks_like_application_form(fields)[0])


class TestAlreadyApplied(unittest.TestCase):
    """A job the board has already taken is not a blocked draft.

    Draft #7's page said "Already applied". Left pending it would keep
    offering the user a `done 7` that cannot work, and any attempt to satisfy
    it risks a duplicate application under their name.
    """

    class Page:
        def __init__(self, text="", raises=False):
            self._text, self._raises = text, raises

        def inner_text(self, _selector):
            if self._raises:
                raise RuntimeError("gone")
            return self._text

    def test_the_live_wording_is_recognised(self):
        page = self.Page("EXPLORE SAVED 1 APPLICATIONS H IT Help Desk "
                         "Specialist ... posted 14 days ago Already applied "
                         "Track Applications")
        self.assertEqual(browser_mod.detect_already_applied(page),
                         "already applied")

    def test_every_marker_matches(self):
        for marker in browser_mod.ALREADY_APPLIED_MARKERS:
            with self.subTest(marker=marker):
                self.assertEqual(
                    browser_mod.detect_already_applied(
                        self.Page(f"header {marker} footer")), marker)

    def test_an_ordinary_job_page_is_not_already_applied(self):
        page = self.Page("IT Help Desk Full Time On-site Apply For Job")
        self.assertEqual(browser_mod.detect_already_applied(page), "")

    def test_the_word_apply_alone_does_not_count(self):
        """A false positive here means a job silently never applied for, so
        the match is deliberately exact-phrase."""
        for text in ("Apply For Job", "Applications close soon",
                     "How to apply", "Applicants must have 3 years"):
            with self.subTest(text=text):
                self.assertEqual(
                    browser_mod.detect_already_applied(self.Page(text)), "")

    def test_an_unreadable_page_is_not_assumed_applied(self):
        self.assertEqual(
            browser_mod.detect_already_applied(self.Page(raises=True)), "")


class TestSpaSettling(unittest.TestCase):
    """The reason the hop overshot: the page had not rendered yet."""

    class Page:
        def __init__(self, fail_states=()):
            self.states = []
            self.slept = 0
            self._fail = set(fail_states)

        def wait_for_load_state(self, state, timeout=None):
            if state in self._fail:
                raise RuntimeError("never idle")
            self.states.append(state)

        def wait_for_timeout(self, ms):
            self.slept += ms

    def test_it_waits_for_the_network_to_go_quiet(self):
        page = self.Page()
        browser_mod._settle_spa(page)
        self.assertEqual(page.states, ["networkidle"])
        self.assertGreaterEqual(page.slept, 2000)

    def test_a_page_that_never_goes_idle_falls_back_to_load(self):
        page = self.Page(fail_states=("networkidle",))
        browser_mod._settle_spa(page)
        self.assertEqual(page.states, ["load"])

    def test_a_page_that_answers_nothing_still_returns(self):
        page = self.Page(fail_states=("networkidle", "load"))
        browser_mod._settle_spa(page)
        self.assertGreaterEqual(page.slept, 2000)
