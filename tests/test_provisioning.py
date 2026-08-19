"""
Multi-platform provisioning: derived credentials, the profile payload, and how
far the registration flow is allowed to go on its own.

TWO THINGS THIS FILE GUARDS
---------------------------
1. ONE BREACH MUST NOT BE REPLAYABLE. Every board gets a DIFFERENT password,
   derived from one seed by HMAC. If that derivation ever collapses to a shared
   value, six accounts fall to one leak -- and nothing else in the system would
   notice, because a shared password works perfectly.

2. A CAPTCHA IS WHERE THE BOT STOPS. Pushing through a human-verification
   challenge is what turns "an account" into "a banned account on a board the
   job hunt depends on". Auto-submit is genuinely useful on the forms that have
   no challenge; on the ones that do, stopping is the whole point.

Playwright is faked. The derivation, the payload resolution and the submit/stop
decision are not.

Run:  python -m pytest tests/test_provisioning.py -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto_apply.browser as browser_mod                         # noqa: E402
import auto_apply.candidate as candidate_mod                     # noqa: E402
from auto_apply.browser import FormField, click_submit           # noqa: E402
from auto_apply.candidate import CandidateProfile                # noqa: E402
from auto_apply.profile_builder import (                         # noqa: E402
    Platform, ProfilePayload, build_profile, configured_platforms,
    derive_password, find_platform, format_account_message,
    prefill_registration, provision, provision_all,
)
from conftest import RecordingNotifier                           # noqa: E402
from config import settings                                      # noqa: E402
from vault import SecureStore                                    # noqa: E402

PROFILE = CandidateProfile(
    full_name="Hossam Eldefrawy",
    email="hossam.eldefrawy.dev@gmail.com",
    phone="+201000025860",
    location="Cairo, Egypt",
    headline="IT Support Engineer",
    summary="IT Application Support Engineer with VoIP and Asterisk experience.",
    years_experience=3.0,
)

SIGNUP_FORM = [
    FormField(selector="#name", kind="full_name", input_type="text",
              label="Full name"),
    FormField(selector="#email", kind="email", input_type="text",
              label="Email address"),
    FormField(selector="#user", kind="username", input_type="text",
              label="Username"),
    FormField(selector="#pw", kind="password", input_type="text",
              label="Password"),
    FormField(selector="#phone", kind="phone", input_type="text",
              label="Mobile number"),
    FormField(selector="#city", kind="location", input_type="text",
              label="City"),
    FormField(selector="#headline", kind="headline", input_type="text",
              label="Professional headline"),
    FormField(selector="#bio", kind="bio", input_type="textarea",
              label="Professional summary"),
    FormField(selector="#cv", kind="resume", input_type="file",
              label="Upload CV"),
]


class FakeContext:
    """Stands in for a Playwright BrowserContext -- only the session matters."""

    def __init__(self):
        self.saved_for: list[str] = []


class FakePage:
    def __init__(self, markup="<form></form>", submittable=True):
        self.markup = markup
        self.submittable = submittable
        self.visited: list[str] = []
        self.filled: list[tuple[str, str]] = []
        self.clicked: list[str] = []
        self.screenshots: list[str] = []

    def goto(self, url, **kwargs):
        self.visited.append(url)

    def content(self):
        return self.markup

    def click(self, selector, **kwargs):
        if not self.submittable:
            raise RuntimeError("no such element")
        self.clicked.append(selector)

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def wait_for_timeout(self, *args, **kwargs):
        pass


@contextmanager
def faked_browser(page, form=None):
    """Replace Playwright and the CV extraction for the duration of a block."""
    saved = {
        "browser_context": browser_mod.browser_context,
        "browser_page": browser_mod.browser_page,
        "inspect_form": browser_mod.inspect_form,
        "fill_field": browser_mod.fill_field,
        "capture_evidence": browser_mod.capture_evidence,
        "save_storage_state": browser_mod.save_storage_state,
        "load_candidate": candidate_mod.load_candidate,
    }

    context = FakeContext()

    @contextmanager
    def fake_context(platform="default", headed=None, use_state=True):
        yield context, page

    @contextmanager
    def fake_page(platform="default", headed=None, use_state=True):
        yield page

    def fake_fill(target, field_, value):
        target.filled.append((field_.kind, value))
        return True

    def fake_save(ctx, platform):
        ctx.saved_for.append(platform)
        return f"/secrets/sessions/{platform}_state.json"

    browser_mod.browser_context = fake_context
    browser_mod.browser_page = fake_page
    browser_mod.inspect_form = lambda p, *a, **k: list(
        SIGNUP_FORM if form is None else form
    )
    browser_mod.fill_field = fake_fill
    browser_mod.capture_evidence = lambda p, prefix: f"/shots/{prefix}.png"
    browser_mod.save_storage_state = fake_save
    candidate_mod.load_candidate = lambda *a, **k: PROFILE
    page.context = context
    try:
        yield
    finally:
        for name, original in saved.items():
            target = candidate_mod if name == "load_candidate" else browser_mod
            setattr(target, name, original)


@contextmanager
def candidate(profile=PROFILE):
    original = candidate_mod.load_candidate
    candidate_mod.load_candidate = lambda *a, **k: profile
    try:
        yield
    finally:
        candidate_mod.load_candidate = original


# ---------------------------------------------------------------------------
class TestDerivedCredentials(unittest.TestCase):
    BOARDS = ["Tanqeeb", "Wuzzuf", "Talent.com", "GulfTalent", "Bayt",
              "Naukrigulf"]

    def test_every_board_gets_a_different_password(self):
        """One board's breach must not be replayable against the others."""
        passwords = [derive_password(name) for name in self.BOARDS]
        self.assertEqual(len(set(passwords)), len(self.BOARDS), passwords)

    def test_derivation_is_reproducible_from_the_seed(self):
        """The vault is a convenience, not the only copy."""
        self.assertEqual(derive_password("Tanqeeb"), derive_password("Tanqeeb"))
        self.assertEqual(derive_password("tanqeeb"), derive_password("TANQEEB"),
                         "case in the board name must not fork the password")

    def test_passwords_meet_the_complexity_rules_boards_enforce(self):
        for name in self.BOARDS:
            with self.subTest(board=name):
                pw = derive_password(name)
                self.assertGreaterEqual(len(pw), 16)
                self.assertTrue(any(c.isupper() for c in pw))
                self.assertTrue(any(c.islower() for c in pw))
                self.assertTrue(any(c.isdigit() for c in pw))
                self.assertTrue(any(not c.isalnum() for c in pw))

    def test_the_seed_is_not_recoverable_from_any_output(self):
        for name in self.BOARDS:
            with self.subTest(board=name):
                self.assertNotIn("TestSeed99@", derive_password(name))

    def test_a_missing_seed_is_an_error_not_a_weak_password(self):
        """Silently falling back to a constant would give every board the same
        password and nothing would look wrong."""
        original = settings.apply_base_password
        settings.apply_base_password = ""
        try:
            with self.assertRaises(RuntimeError):
                derive_password("Tanqeeb")
        finally:
            settings.apply_base_password = original

    def test_the_username_is_derived_from_the_email_handle(self):
        self.assertEqual(PROFILE.username, "hossameldefrawydev")

    def test_the_username_falls_back_to_the_name(self):
        bare = CandidateProfile(full_name="Hossam Eldefrawy", email="h@x.com")
        self.assertEqual(bare.username, "hossameldefrawy")

    def test_the_username_is_safe_for_a_form_field(self):
        odd = CandidateProfile(full_name="Hossam El-Defrawy",
                               email="hossam+jobs@example.com")
        self.assertTrue(odd.username.isalnum())
        self.assertLessEqual(len(odd.username), 24)


# ---------------------------------------------------------------------------
class TestProfilePayload(unittest.TestCase):
    PLATFORM = Platform(name="Tanqeeb", url="https://www.tanqeeb.com/register")

    def test_the_payload_carries_everything_a_signup_form_asks_for(self):
        with candidate():
            profile = build_profile(self.PLATFORM)
        self.assertIsInstance(profile, ProfilePayload)
        self.assertEqual(profile.full_name, "Hossam Eldefrawy")
        self.assertTrue(profile.email)
        self.assertTrue(profile.username)
        self.assertTrue(profile.password)
        self.assertTrue(profile.phone)
        self.assertTrue(profile.location)
        self.assertTrue(profile.headline)
        self.assertTrue(profile.bio)

    def test_config_identity_beats_the_model(self):
        """The model should not have the last word on how you describe
        yourself, or on your own contact details."""
        original = dict(settings.raw.get("auto_apply", {}) or {})
        settings.raw["auto_apply"] = dict(original, identity={
            "email": "override@example.com",
            "headline": "VoIP & Network Engineer / AI Graduate",
        })
        try:
            with candidate():
                profile = build_profile(self.PLATFORM)
        finally:
            settings.raw["auto_apply"] = original
        self.assertEqual(profile.email, "override@example.com")
        self.assertEqual(profile.headline,
                         "VoIP & Network Engineer / AI Graduate")

    def test_form_values_map_onto_the_semantic_field_names(self):
        with candidate():
            values = build_profile(self.PLATFORM).form_values()
        for kind in ("full_name", "email", "username", "password", "phone",
                     "location", "headline", "bio"):
            with self.subTest(kind=kind):
                self.assertTrue(values.get(kind), kind)

    def test_empty_values_are_dropped_rather_than_typed_as_blanks(self):
        with candidate(CandidateProfile(full_name="X", email="x@y.com")):
            values = build_profile(self.PLATFORM).form_values()
        self.assertNotIn("linkedin", values)

    def test_each_board_gets_its_own_password_in_its_own_payload(self):
        with candidate():
            first = build_profile(Platform(name="Tanqeeb", url="u"))
            second = build_profile(Platform(name="Bayt", url="u"))
        self.assertNotEqual(first.password, second.password)


# ---------------------------------------------------------------------------
class TestConfiguredPlatforms(unittest.TestCase):
    def test_the_boards_the_spec_asks_for_are_configured(self):
        names = {p.name.lower() for p in configured_platforms()}
        for expected in ("tanqeeb", "wuzzuf", "talent.com", "gulftalent",
                         "bayt"):
            with self.subTest(board=expected):
                self.assertIn(expected, names)

    def test_every_platform_has_a_registration_url(self):
        for platform in configured_platforms():
            with self.subTest(board=platform.name):
                self.assertTrue(platform.url.startswith("https://"))

    def test_phone_verified_boards_are_marked_manual(self):
        """A half-created account on these burns the email address."""
        manual = {p.name.lower() for p in configured_platforms()
                  if p.manual_signup}
        self.assertIn("bayt", manual)

    def test_lookup_by_name_is_case_insensitive(self):
        self.assertIsNotNone(find_platform("tanqeeb"))
        self.assertIsNotNone(find_platform("GULFTALENT"))
        self.assertIsNone(find_platform("nosuchboard"))


# ---------------------------------------------------------------------------
class VaultHarness(unittest.TestCase):
    def setUp(self):
        self.store = SecureStore(Path(tempfile.mkdtemp()) / "vault.db")
        self.notifier = RecordingNotifier()

    def tearDown(self):
        self.store.close()


class TestProvisioning(VaultHarness):
    PLATFORM = Platform(name="GulfTalent",
                        url="https://www.gulftalent.com/register")

    def test_provision_vaults_platform_email_username_and_password(self):
        with candidate():
            profile = provision(self.PLATFORM, self.store)
        stored = self.store.get_credentials("GulfTalent")
        self.assertEqual(stored["platform_name"], "GulfTalent")
        self.assertEqual(stored["email"], profile.email)
        self.assertEqual(stored["username"], profile.username)
        self.assertEqual(stored["password"], profile.password)

    def test_provision_opens_no_browser_and_sends_nothing(self):
        """It has to be safe to run for every board at once."""
        opened = []
        original = browser_mod.browser_page
        browser_mod.browser_page = lambda *a, **k: opened.append(1)
        try:
            with candidate():
                provision(self.PLATFORM, self.store, self.notifier)
        finally:
            browser_mod.browser_page = original
        self.assertEqual(opened, [])
        self.assertEqual(self.notifier.telegram, [])
        self.assertEqual(self.notifier.whatsapp, [])

    def test_the_password_is_not_plaintext_on_disk(self):
        with candidate():
            profile = provision(self.PLATFORM, self.store)
        self.assertNotIn(profile.password.encode(), self.store.path.read_bytes())

    def test_re_provisioning_does_not_reset_a_completed_profile(self):
        """Re-running the sync must not un-finish an account you already made."""
        with candidate():
            provision(self.PLATFORM, self.store)
            self.store.set_profile_status("GulfTalent", "complete")
            provision(self.PLATFORM, self.store)
        self.assertEqual(
            self.store.get_credentials("GulfTalent")["profile_status"],
            "complete",
        )

    def test_a_real_username_from_the_board_survives_a_re_provision(self):
        """Boards hand you your actual handle only after signup; the derived
        one must never overwrite it."""
        with candidate():
            provision(self.PLATFORM, self.store)
            profile = build_profile(self.PLATFORM)
            self.store.save_credentials(
                platform_name="GulfTalent", platform_url=self.PLATFORM.url,
                email=profile.email, password=profile.password,
                username="hossam_gt_9931",
            )
            provision(self.PLATFORM, self.store)
        self.assertEqual(self.store.get_credentials("GulfTalent")["username"],
                         "hossam_gt_9931")

    def test_provision_all_covers_every_configured_board(self):
        with candidate():
            profiles = provision_all(self.store)
        self.assertEqual(len(profiles), len(configured_platforms()))
        vaulted = {row["platform_name"] for row in self.store.list_platforms()}
        self.assertEqual(vaulted,
                         {p.name for p in configured_platforms()})

    def test_provision_all_gives_every_board_a_distinct_password(self):
        with candidate():
            profiles = provision_all(self.store)
        passwords = [p.password for p in profiles]
        self.assertEqual(len(set(passwords)), len(passwords))

    def test_listing_the_vault_never_exposes_a_password(self):
        with candidate():
            provision(self.PLATFORM, self.store)
        for row in self.store.list_platforms():
            self.assertNotIn("password", row)
            self.assertNotIn("password_encrypted", row)


# ---------------------------------------------------------------------------
class TestRegistrationFlow(VaultHarness):
    PLATFORM = Platform(name="Tanqeeb", url="https://www.tanqeeb.com/register")
    CAPTCHA_PAGE = '<form><div class="g-recaptcha" data-sitekey="x"></div></form>'

    def test_every_profile_field_is_pre_filled(self):
        page = FakePage()
        with faked_browser(page):
            report = prefill_registration(self.PLATFORM, self.store,
                                          self.notifier, headed=False)
        filled = dict(page.filled)
        self.assertEqual(filled["full_name"], "Hossam Eldefrawy")
        self.assertTrue(filled["email"])
        self.assertTrue(filled["username"])
        self.assertTrue(filled["password"])
        self.assertTrue(filled["phone"])
        self.assertTrue(filled["location"])
        self.assertTrue(report["filled"])

    def test_the_headline_and_bio_are_filled_not_left_blank(self):
        """These are what make a profile readable to a recruiter; a stub
        profile never surfaces in a board's own search."""
        page = FakePage()
        with faked_browser(page):
            prefill_registration(self.PLATFORM, self.store, self.notifier,
                                 headed=False)
        with candidate():
            expected = build_profile(self.PLATFORM)
        filled = dict(page.filled)
        # The configured identity headline wins over the CV-derived one -- the
        # model should not have the last word on how you describe yourself.
        self.assertEqual(filled["headline"], expected.headline)
        self.assertEqual(filled["bio"], PROFILE.summary)
        self.assertTrue(filled["headline"])

    def test_the_cv_is_uploaded(self):
        page = FakePage()
        with faked_browser(page):
            report = prefill_registration(self.PLATFORM, self.store,
                                          self.notifier, headed=False)
        self.assertIn("resume (PDF uploaded)", report["filled"])
        self.assertTrue(dict(page.filled)["resume"].lower().endswith(".pdf"))

    def test_credentials_are_vaulted_before_the_browser_opens(self):
        """An interrupted signup must still leave a recoverable password, not
        one that existed only in a window that got closed."""
        seen: list[str] = []

        page = FakePage()

        def exploding_goto(url, **kwargs):
            seen.append(
                (self.store.get_credentials("Tanqeeb") or {}).get("password", "")
            )
            raise RuntimeError("the page never loaded")

        page.goto = exploding_goto
        with faked_browser(page):
            with self.assertRaises(RuntimeError):
                prefill_registration(self.PLATFORM, self.store, self.notifier,
                                     headed=False)
        self.assertTrue(seen and seen[0],
                        "the vault was empty at the moment the browser opened")

    def test_a_clean_form_is_submitted_automatically(self):
        page = FakePage(markup="<form><input name='email'></form>")
        with faked_browser(page):
            report = prefill_registration(self.PLATFORM, self.store,
                                          self.notifier, headed=False)
        self.assertTrue(report["submitted"])
        self.assertEqual(report["status"], "complete")
        self.assertTrue(page.clicked)

    def test_a_captcha_stops_the_bot_dead(self):
        """Defeating a human-verification challenge is what gets accounts
        banned. Stopping is the entire point of detecting it."""
        page = FakePage(markup=self.CAPTCHA_PAGE)
        with faked_browser(page):
            report = prefill_registration(self.PLATFORM, self.store,
                                          self.notifier, headed=False)
        self.assertFalse(report["submitted"])
        self.assertEqual(page.clicked, [])
        self.assertEqual(report["status"], "awaiting_user_submit")
        self.assertIn("reCAPTCHA", report["captcha"])

    def test_a_captcha_page_is_still_fully_pre_filled(self):
        """You solve the challenge; you should not also retype the form."""
        page = FakePage(markup=self.CAPTCHA_PAGE)
        with faked_browser(page):
            report = prefill_registration(self.PLATFORM, self.store,
                                          self.notifier, headed=False)
        self.assertIn("headline", report["filled"])
        self.assertIn("bio", report["filled"])

    def test_a_manual_signup_board_is_never_auto_submitted(self):
        page = FakePage(markup="<form><input name='email'></form>")
        bayt = Platform(name="Bayt", url="https://www.bayt.com/en/register/",
                        manual_signup=True)
        with faked_browser(page):
            report = prefill_registration(bayt, self.store, self.notifier,
                                          headed=False)
        self.assertFalse(report["submitted"])
        self.assertEqual(page.clicked, [])
        self.assertTrue(report["filled"], "it should still be pre-filled")

    def test_the_config_switch_disables_auto_submit_everywhere(self):
        page = FakePage(markup="<form><input name='email'></form>")
        original = dict(settings.raw.get("auto_apply", {}) or {})
        settings.raw["auto_apply"] = dict(original,
                                          auto_submit_registration=False)
        try:
            with faked_browser(page):
                report = prefill_registration(self.PLATFORM, self.store,
                                              self.notifier, headed=False)
        finally:
            settings.raw["auto_apply"] = original
        self.assertFalse(report["submitted"])
        self.assertEqual(page.clicked, [])

    def test_a_form_with_no_submit_control_hands_back_to_you(self):
        page = FakePage(markup="<form></form>", submittable=False)
        with faked_browser(page):
            report = prefill_registration(self.PLATFORM, self.store,
                                          self.notifier, headed=False)
        self.assertFalse(report["submitted"])
        self.assertEqual(report["status"], "awaiting_user_submit")

    def test_the_account_card_reaches_both_channels(self):
        page = FakePage(markup="<form><input name='email'></form>")
        with faked_browser(page):
            prefill_registration(self.PLATFORM, self.store, self.notifier,
                                 headed=False)
        self.assertEqual(len(self.notifier.telegram), 1)
        self.assertEqual(len(self.notifier.whatsapp), 1)
        self.assertIn("NEW PLATFORM ACCOUNT CREATED",
                      self.notifier.last_telegram)

    def test_the_account_card_names_the_username_and_headline(self):
        with candidate():
            profile = build_profile(self.PLATFORM)
        message = format_account_message(self.PLATFORM, profile, "ready",
                                         ["email", "headline"])
        self.assertIn(profile.username, message)
        self.assertIn(profile.headline, message)
        self.assertIn(profile.password, message)
        self.assertIn("Saved Messages", message)

    def test_evidence_is_captured_either_way(self):
        for markup in ("<form><input name='email'></form>", self.CAPTCHA_PAGE):
            with self.subTest(markup=markup[:20]):
                page = FakePage(markup=markup)
                with faked_browser(page):
                    report = prefill_registration(self.PLATFORM, self.store,
                                                  self.notifier, headed=False)
                self.assertTrue(report["screenshot"])


# ---------------------------------------------------------------------------
class TestSharedSubmitControl(unittest.TestCase):
    """Registration and application press the button through one function.

    They used to be two lists that had already drifted: the application flow
    knew the Arabic "تقديم" and the registration flow knew none of it, so an
    Arabic-rendered signup page had no submit button as far as the bot was
    concerned.
    """

    def test_the_first_matching_selector_wins(self):
        page = FakePage()
        self.assertEqual(click_submit(page), 'button[type="submit"]')

    def test_arabic_submit_labels_are_covered(self):
        for label in ("تقديم", "إرسال", "تسجيل", "إنشاء حساب"):
            with self.subTest(label=label):
                self.assertTrue(
                    any(label in s for s in browser_mod.SUBMIT_SELECTORS)
                )

    def test_english_signup_labels_are_covered(self):
        for label in ("Register", "Sign up", "Create account", "Apply",
                      "Submit"):
            with self.subTest(label=label):
                self.assertTrue(
                    any(label in s for s in browser_mod.SUBMIT_SELECTORS)
                )

    def test_no_button_returns_empty_rather_than_raising(self):
        self.assertEqual(click_submit(FakePage(submittable=False)), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
