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
import unittest
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
