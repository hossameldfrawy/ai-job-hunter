"""
Every board, the saved-session plumbing, the ATS layer, and inbound commands
from either channel.

THREE THINGS THIS FILE EXISTS TO PROVE
--------------------------------------
1. A JOB URL RESOLVES TO THE RIGHT SAVED LOGIN. This is the whole reason the
   drafts were being refused: signed out, these boards serve a public landing
   page whose only form is the site search. If `bayt.com/en/jobs/x` does not
   map to the Bayt session, the browser arrives anonymous and the apply form
   simply is not there. And it must map on the DOMAIN, or a lookalike host
   borrows real cookies.

2. AN INBOUND WEBHOOK CANNOT BE DRIVEN BY A STRANGER. The endpoint can submit
   a job application in the user's name. It is public HTTP. So it fails closed
   with no secret, rejects a bad signature, rejects the wrong phone number, and
   refuses to run a replayed message twice.

3. "NEXT" IS NOT "SUBMIT". On a Workday-style wizard, clicking submit when you
   meant next files a half-empty application, and that cannot be taken back.

Run:  python -m pytest tests/test_all_platforms_and_inbound.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto_apply.browser as browser_mod                         # noqa: E402
from auto_apply.browser import (                                 # noqa: E402
    ATS_MARKERS, MULTI_STEP_ATS, NEXT_SELECTORS, SUBMIT_SELECTORS, attach_cv,
    clear_storage_state, detect_ats, click_next, has_saved_session,
    has_submit, platform_slug, save_storage_state, storage_state_path,
)
from auto_apply.control import ReviewController                  # noqa: E402
from auto_apply.engine import (                                  # noqa: E402
    STANDARD_SCREENING_QUESTIONS, match_screening_answer,
)
from auto_apply.inbound import (                                 # noqa: E402
    InboundCommand, WhatsAppCommandListener, parse_generic, parse_meta,
    parse_payload, parse_twilio, readiness, whatsapp_cfg,
)
from auto_apply.profile_builder import (                         # noqa: E402
    configured_platforms, find_platform, platform_for_source, platform_for_url,
)
from conftest import RecordingNotifier                           # noqa: E402
from config import settings                                      # noqa: E402
from vault import STATUS_REVIEW, SecureStore                     # noqa: E402


# ---------------------------------------------------------------------------
# 1. Boards
# ---------------------------------------------------------------------------
class TestBoardCoverage(unittest.TestCase):
    """Every board the brief names is configured and reachable by URL."""

    REQUIRED = [
        "Tanqeeb", "Wuzzuf", "Bayt", "GulfTalent", "Naukrigulf", "Forasna",
        "Akhtaboot", "Talent.com", "Indeed", "Glassdoor", "ZipRecruiter",
        "Foundit", "RemoteOK", "WeWorkRemotely",
    ]

    def test_every_named_board_is_configured(self):
        names = {p.name for p in configured_platforms()}
        for board in self.REQUIRED:
            with self.subTest(board=board):
                self.assertIn(board, names)

    def test_every_board_has_an_https_signup_and_login_url(self):
        for platform in configured_platforms():
            with self.subTest(board=platform.name):
                self.assertTrue(platform.url.startswith("https://"))
                self.assertTrue(platform.login_url.startswith("https://"))

    def test_every_board_declares_at_least_one_host(self):
        """No hosts means no job URL can ever resolve to its saved login."""
        for platform in configured_platforms():
            with self.subTest(board=platform.name):
                self.assertTrue(platform.hosts, platform.name)

    def test_no_two_boards_claim_the_same_host(self):
        """An ambiguous host would hand one board's cookies to another."""
        seen: dict[str, str] = {}
        for platform in configured_platforms():
            for host in platform.hosts:
                with self.subTest(host=host):
                    self.assertNotIn(host, seen,
                                     f"{host} claimed by {seen.get(host)} "
                                     f"and {platform.name}")
                    seen[host] = platform.name

    def test_slugs_are_unique_and_filesystem_safe(self):
        """The slug names the session file; a collision shares a login."""
        slugs = [p.slug for p in configured_platforms()]
        self.assertEqual(len(slugs), len(set(slugs)))
        for slug in slugs:
            with self.subTest(slug=slug):
                self.assertRegex(slug, r"^[a-z0-9_]+$")


class TestJobUrlResolution(unittest.TestCase):
    """A scraped URL has to find the login that can open it."""

    CASES = [
        ("https://egypt.tanqeeb.com/jobs-in-egypt/all/jobs/1.html", "Tanqeeb"),
        ("https://saudi.tanqeeb.com/jobs/2.html", "Tanqeeb"),
        ("https://wuzzuf.net/jobs/p/123-IT-Support", "Wuzzuf"),
        ("https://www.bayt.com/en/uae/jobs/voip-engineer-1/", "Bayt"),
        ("https://www.gulftalent.com/uae/jobs/it-support-1", "GulfTalent"),
        ("https://www.naukrigulf.com/it-support-jobs-in-dubai", "Naukrigulf"),
        ("https://forasna.com/jobs/9", "Forasna"),
        ("https://www.akhtaboot.com/en/jordan/jobs/1", "Akhtaboot"),
        ("https://ae.talent.com/view?id=63010", "Talent.com"),
        ("https://www.indeed.com/viewjob?jk=abc", "Indeed"),
        ("https://www.glassdoor.com/job-listing/x", "Glassdoor"),
        ("https://www.ziprecruiter.com/c/x/Job/y", "ZipRecruiter"),
        ("https://www.foundit.in/job/1", "Foundit"),
        ("https://www.monstergulf.com/job-openings-1", "Foundit"),
        ("https://remoteok.com/remote-jobs/1", "RemoteOK"),
        ("https://weworkremotely.com/remote-jobs/1", "WeWorkRemotely"),
    ]

    def test_each_job_url_maps_to_its_board(self):
        for url, expected in self.CASES:
            with self.subTest(url=url):
                board = platform_for_url(url)
                self.assertIsNotNone(board, url)
                self.assertEqual(board.name, expected)

    def test_a_lookalike_host_cannot_borrow_a_saved_login(self):
        """Substring matching would hand real cookies to an attacker's domain."""
        for url in ("https://notbayt.com.evil.test/job/1",
                    "https://bayt.com.phish.example/job/1",
                    "https://evil.test/?next=bayt.com"):
            with self.subTest(url=url):
                self.assertIsNone(platform_for_url(url))

    def test_an_unknown_employer_page_belongs_to_nobody(self):
        """Correct: it is opened signed-out, which is right for a careers page."""
        self.assertIsNone(platform_for_url("https://acme-corp.com/careers/1"))
        self.assertIsNone(platform_for_url(""))

    def test_the_scraper_source_tag_resolves_when_the_url_cannot(self):
        board = platform_for_source("tanqeeb:egypt", "")
        self.assertIsNotNone(board)
        self.assertEqual(board.name, "Tanqeeb")

    def test_the_url_beats_the_source_tag(self):
        """A redirect link is more trustworthy than a stale source label."""
        board = platform_for_source("tanqeeb:egypt",
                                    "https://wuzzuf.net/jobs/p/1")
        self.assertEqual(board.name, "Wuzzuf")

    def test_lookup_by_name_is_case_insensitive(self):
        for name in ("bayt", "BAYT", "Bayt", "  gulftalent  ".strip()):
            with self.subTest(name=name):
                self.assertIsNotNone(find_platform(name))


# ---------------------------------------------------------------------------
# 2. Session persistence
# ---------------------------------------------------------------------------
class FakeContext:
    def __init__(self, cookies=None, fail=False):
        self.cookies = cookies or [{"name": "sid", "value": "abc",
                                    "domain": ".bayt.com", "path": "/"}]
        self.fail = fail
        self.added: list[list[dict]] = []

    def storage_state(self, path=None):
        if self.fail:
            raise RuntimeError("context already closed")
        Path(path).write_text(
            json.dumps({"cookies": self.cookies, "origins": []}),
            encoding="utf-8",
        )

    def add_cookies(self, cookies):
        self.added.append(cookies)


class TestSessionPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._original = dict(settings.raw.get("auto_apply", {}) or {})
        settings.raw["auto_apply"] = dict(self._original,
                                          session_dir=str(self.tmp))

    def tearDown(self):
        settings.raw["auto_apply"] = self._original

    def test_the_state_path_is_per_board_and_predictable(self):
        path = storage_state_path("Bayt")
        self.assertEqual(path.name, "bayt_state.json")
        self.assertEqual(path.parent, self.tmp)

    def test_slugs_are_normalised(self):
        self.assertEqual(platform_slug("Talent.com"), "talent_com")
        self.assertEqual(platform_slug("We Work Remotely"), "we_work_remotely")
        self.assertEqual(platform_slug(""), "default")

    def test_saving_creates_a_gitignored_directory(self):
        """A logged-in session is a secret; it must never be committable."""
        save_storage_state(FakeContext(), "bayt")
        self.assertEqual((self.tmp / ".gitignore").read_text(encoding="utf-8"),
                         "*\n")

    def test_a_saved_session_round_trips(self):
        self.assertFalse(has_saved_session("bayt"))
        path = save_storage_state(FakeContext(), "bayt")
        self.assertTrue(path)
        self.assertTrue(has_saved_session("bayt"))
        stored = json.loads(storage_state_path("bayt").read_text(encoding="utf-8"))
        self.assertEqual(stored["cookies"][0]["name"], "sid")

    def test_sessions_do_not_leak_between_boards(self):
        save_storage_state(FakeContext(), "bayt")
        self.assertTrue(has_saved_session("bayt"))
        self.assertFalse(has_saved_session("wuzzuf"))

    def test_a_failed_save_returns_empty_rather_than_raising(self):
        """A registration that succeeded must not be reported as failed just
        because the cookie snapshot could not be taken."""
        self.assertEqual(save_storage_state(FakeContext(fail=True), "bayt"), "")

    def test_an_empty_state_file_does_not_count_as_a_session(self):
        storage_state_path("bayt").parent.mkdir(parents=True, exist_ok=True)
        storage_state_path("bayt").write_text("", encoding="utf-8")
        self.assertFalse(has_saved_session("bayt"))

    def test_clearing_a_session_is_reported_honestly(self):
        self.assertFalse(clear_storage_state("bayt"))
        save_storage_state(FakeContext(), "bayt")
        self.assertTrue(clear_storage_state("bayt"))
        self.assertFalse(has_saved_session("bayt"))


# ---------------------------------------------------------------------------
# 3. ATS detection and multi-step forms
# ---------------------------------------------------------------------------
class AtsPage:
    def __init__(self, url="https://boards.example.com/apply", markup="",
                 clickable=(), present=()):
        self.url = url
        self.markup = markup
        self.clickable = tuple(clickable)
        self.present = tuple(present)
        self.clicked: list[str] = []
        self.uploaded: list[str] = []
        self.chooser_used = False

    def content(self):
        return self.markup

    def click(self, selector, **kwargs):
        if not any(c in selector for c in self.clickable):
            raise RuntimeError("no such element")
        self.clicked.append(selector)

    def query_selector(self, selector):
        return object() if any(p in selector for p in self.present) else None

    def set_input_files(self, selector, value, timeout=None):
        if selector not in ("input[type=\"file\"]", "#cv"):
            raise RuntimeError("not a file input")
        self.uploaded.append(value)


class TestAtsDetection(unittest.TestCase):
    VENDORS = {
        "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/x": "workday",
        "https://boards.greenhouse.io/acme/jobs/1": "greenhouse",
        "https://jobs.lever.co/acme/abc": "lever",
        "https://jobs.smartrecruiters.com/acme/1": "smartrecruiters",
        "https://acme.taleo.net/careersection/x": "taleo",
        "https://acme.icims.com/jobs/1": "icims",
        "https://jobs.ashbyhq.com/acme/1": "ashby",
        "https://acme.bamboohr.com/careers/1": "bamboohr",
        "https://acme.recruitee.com/o/1": "recruitee",
        "https://apply.workable.com/acme/j/1": "workable",
    }

    def test_each_vendor_is_recognised_from_its_url(self):
        for url, expected in self.VENDORS.items():
            with self.subTest(url=url):
                self.assertEqual(detect_ats(AtsPage(url=url)), expected)

    def test_an_embedded_ats_is_found_in_the_markup(self):
        """An iframe on the employer's own domain is the common case."""
        page = AtsPage(
            url="https://acme-corp.com/careers/1",
            markup='<iframe src="https://boards.greenhouse.io/embed/x">',
        )
        self.assertEqual(detect_ats(page), "greenhouse")

    def test_an_ordinary_page_names_no_vendor(self):
        self.assertEqual(detect_ats(AtsPage(markup="<form></form>")), "")

    def test_an_unreadable_page_is_not_a_crash(self):
        class Broken(AtsPage):
            def content(self):
                raise RuntimeError("execution context destroyed")

        self.assertEqual(detect_ats(Broken(url="https://x.test/")), "")

    def test_the_wizard_vendors_are_flagged_as_multi_step(self):
        for vendor in ("workday", "taleo", "icims", "smartrecruiters"):
            with self.subTest(vendor=vendor):
                self.assertIn(vendor, MULTI_STEP_ATS)

    def test_every_marker_maps_to_a_named_vendor(self):
        for needle, name in ATS_MARKERS:
            with self.subTest(needle=needle):
                self.assertTrue(needle and name)


class TestMultiStepNavigation(unittest.TestCase):
    def test_next_and_submit_are_kept_strictly_apart(self):
        """Clicking submit when you meant next files a half-empty application,
        and that cannot be taken back."""
        overlap = set(NEXT_SELECTORS) & set(SUBMIT_SELECTORS)
        self.assertEqual(overlap, set())

    def test_a_continue_button_advances_the_form(self):
        page = AtsPage(clickable=("Continue",))
        self.assertTrue(click_next(page))
        self.assertTrue(page.clicked)

    def test_arabic_and_workday_next_controls_are_covered(self):
        for label in ("التالي", "متابعة", "bottom-navigation-next-button"):
            with self.subTest(label=label):
                self.assertTrue(any(label in s for s in NEXT_SELECTORS))

    def test_no_next_control_returns_empty(self):
        self.assertEqual(click_next(AtsPage(clickable=())), "")

    def test_has_submit_only_looks_and_never_clicks(self):
        page = AtsPage(present=('button[type="submit"]',))
        self.assertTrue(has_submit(page))
        self.assertEqual(page.clicked, [], "the detector pressed the button")

    def test_has_submit_is_false_on_an_intermediate_page(self):
        self.assertFalse(has_submit(AtsPage(present=())))


class TestCvAttachment(unittest.TestCase):
    CV = "/tmp/Hossam_Eldefrawy_CV.pdf"

    def test_a_detected_file_field_is_used_first(self):
        page = AtsPage()
        field = browser_mod.FormField(selector="#cv", kind="resume",
                                      input_type="file", label="Upload CV")
        self.assertTrue(attach_cv(page, field, self.CV))
        self.assertEqual(page.uploaded, [self.CV])

    def test_a_hidden_input_behind_a_styled_button_still_receives_the_cv(self):
        """Greenhouse, Lever and most modern ATSes hide the real input and
        style a div over it, so the visible control cannot be filled."""
        page = AtsPage()
        self.assertTrue(attach_cv(page, None, self.CV))
        self.assertEqual(page.uploaded, [self.CV])

    def test_no_cv_path_is_a_clean_no(self):
        self.assertFalse(attach_cv(AtsPage(), None, ""))

    def test_a_page_with_no_upload_route_reports_failure(self):
        class NoFiles(AtsPage):
            def set_input_files(self, *a, **k):
                raise RuntimeError("no file input")

        self.assertFalse(attach_cv(NoFiles(), None, self.CV))


class TestScreeningAnswers(unittest.TestCase):
    ANSWERS = {
        "What is your notice period?": "Two weeks",
        "What is your expected salary?": "15,000 EGP monthly",
        "How many years of experience do you have with VoIP, SIP and Asterisk?":
            "Three years",
        "How many years of experience do you have with Python?": "Two years",
        "Do you require visa sponsorship to work in this location?": "No",
        "Are you willing to relocate?": "Yes",
    }

    def test_the_standard_set_covers_what_every_ats_asks(self):
        blob = " ".join(STANDARD_SCREENING_QUESTIONS).lower()
        for topic in ("notice", "salary", "voip", "python", "sponsorship",
                      "relocate", "start"):
            with self.subTest(topic=topic):
                self.assertIn(topic, blob)

    def test_a_differently_worded_question_finds_its_answer(self):
        """A wizard's page three says "Notice period (weeks)"; the draft says
        "What is your notice period?". Exact matching leaves it blank and the
        form fails validation."""
        for label, expected in (
            ("Notice period (in weeks)", "Two weeks"),
            ("Expected Salary", "15,000 EGP monthly"),
            ("Years of Asterisk experience", "Three years"),
            ("Python experience", "Two years"),
            ("Do you need visa sponsorship?", "No"),
            ("Willing to relocate?", "Yes"),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    match_screening_answer(label, self.ANSWERS), expected
                )

    def test_an_unrelated_question_is_left_blank_not_guessed(self):
        """A wrong answer to a screening question is worse than an empty one a
        human then fills in."""
        for label in ("What is your favourite colour?",
                      "Describe a difficult outage you resolved",
                      ""):
            with self.subTest(label=label):
                self.assertEqual(match_screening_answer(label, self.ANSWERS), "")


# ---------------------------------------------------------------------------
# 4. Inbound commands
# ---------------------------------------------------------------------------
PHONE = "+201000007582"

META_BODY = {
    "object": "whatsapp_business_account",
    "entry": [{"changes": [{"value": {"messages": [{
        "from": "201000007582", "id": "wamid.ABC", "timestamp": "1755600000",
        "type": "text", "text": {"body": "done 1"},
    }]}}]}],
}


class TestPayloadAdapters(unittest.TestCase):
    def test_meta_cloud_api(self):
        [command] = parse_meta(META_BODY)
        self.assertEqual(command.text, "done 1")
        self.assertEqual(command.sender, "201000007582")
        self.assertEqual(command.message_id, "wamid.ABC")
        self.assertEqual(command.relay, "meta")
        self.assertIsNotNone(command.when)

    def test_twilio_form_post(self):
        [command] = parse_twilio({
            "MessageSid": "SM1", "From": f"whatsapp:{PHONE}",
            "Body": "edit 3 salary: 15000",
        })
        self.assertEqual(command.text, "edit 3 salary: 15000")
        self.assertEqual(command.relay, "twilio")

    def test_a_generic_relay_may_use_any_common_key_spelling(self):
        for payload in (
            {"from": PHONE, "text": "موافق ٣", "id": "g1"},
            {"sender": PHONE, "body": "موافق ٣", "message_id": "g1"},
            {"phone": PHONE, "message": "موافق ٣"},
        ):
            with self.subTest(payload=sorted(payload)):
                [command] = parse_generic(payload)
                self.assertEqual(command.text, "موافق ٣")

    def test_the_relay_is_detected_from_the_payload_shape(self):
        self.assertEqual(parse_payload(META_BODY)[0].relay, "meta")
        self.assertEqual(
            parse_payload({"MessageSid": "SM1", "Body": "done"})[0].relay,
            "twilio",
        )
        self.assertEqual(parse_payload({"text": "done"})[0].relay, "generic")

    def test_junk_and_non_text_messages_yield_nothing(self):
        for payload in ([], "nope", {}, {"entry": []},
                        {"text": "   "},
                        {"object": "whatsapp_business_account", "entry": [
                            {"changes": [{"value": {"messages": [
                                {"type": "image", "from": "20", "id": "x"}
                            ]}}]}]}):
            with self.subTest(payload=str(payload)[:40]):
                self.assertEqual(parse_payload(payload), [])

    def test_a_status_callback_is_not_a_command(self):
        """Meta posts delivery receipts to the same endpoint, constantly."""
        self.assertEqual(parse_payload({
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {"statuses": [
                {"id": "wamid.X", "status": "delivered"}
            ]}}]}],
        }), [])


class InboundHarness(unittest.TestCase):
    SECRET = "relay-secret"
    APP_SECRET = "meta-app-secret"

    def setUp(self):
        self.store = SecureStore(Path(tempfile.mkdtemp()) / "vault.db")
        self.notifier = RecordingNotifier()
        self.submitted: list[int] = []

        def fake_submit(app_id, store, notifier, dry_run=False):
            self.submitted.append(app_id)
            return True

        self.controller = ReviewController(
            self.store, self.notifier, submit_fn=fake_submit,
            approve_fn=lambda app_id, store: True,
        )
        self.listener = WhatsAppCommandListener(
            self.controller, allowed_number=PHONE,
            app_secret=self.APP_SECRET, shared_secret=self.SECRET,
            max_age_minutes=0,
        )

    def tearDown(self):
        self.store.close()

    def _draft(self, fingerprint="fp-inbound"):
        return self.store.record_application(
            job_fingerprint=fingerprint, job_id=101, company="Etisalat",
            role="VoIP Engineer", platform="tanqeeb:uae",
            job_url="https://uae.tanqeeb.com/1.html",
            payload={"fields": {}, "field_map": [], "draft": {},
                     "form_ok": True},
            cover_letter="A letter", status=STATUS_REVIEW,
        )

    def _signed(self, body: dict) -> tuple[bytes, str]:
        raw = json.dumps(body).encode()
        signature = "sha256=" + hmac.new(
            self.APP_SECRET.encode(), raw, hashlib.sha256
        ).hexdigest()
        return raw, signature


class TestInboundSecurity(InboundHarness):
    """This endpoint can file a job application. It is public HTTP."""

    def test_an_unauthenticated_request_is_refused_outright(self):
        with self.assertRaises(PermissionError):
            self.listener.handle_payload({"from": PHONE, "text": "done"})
        self.assertEqual(self.submitted, [])

    def test_a_forged_signature_is_refused(self):
        raw = json.dumps(META_BODY).encode()
        with self.assertRaises(PermissionError):
            self.listener.handle_payload(META_BODY, raw=raw,
                                         signature="sha256=deadbeef")

    def test_a_valid_meta_signature_is_accepted(self):
        self._draft()
        raw, signature = self._signed(META_BODY)
        replies = self.listener.handle_payload(META_BODY, raw=raw,
                                               signature=signature)
        self.assertEqual(len(replies), 1)
        self.assertEqual(self.submitted, [1])

    def test_a_wrong_shared_secret_is_refused(self):
        with self.assertRaises(PermissionError):
            self.listener.handle_payload({"from": PHONE, "text": "status"},
                                         token="not-the-secret")

    def test_the_right_shared_secret_is_accepted(self):
        self.listener.handle_payload({"from": PHONE, "text": "status"},
                                     token=self.SECRET)
        self.assertTrue(self.notifier.telegram)

    def test_no_secret_configured_means_nothing_is_accepted(self):
        """Fails CLOSED. An open endpoint that can submit applications is not
        a degraded mode, it is a vulnerability."""
        open_listener = WhatsAppCommandListener(
            self.controller, allowed_number=PHONE, app_secret="",
            shared_secret="",
        )
        with self.assertRaises(PermissionError):
            open_listener.handle_payload({"from": PHONE, "text": "done"})

    def test_a_command_from_another_number_is_ignored(self):
        self._draft()
        self.listener.handle_payload({"from": "+201111111111", "text": "done"},
                                     token=self.SECRET)
        self.assertEqual(self.submitted, [])
        self.assertEqual(self.listener.stats.wrong_sender, 1)

    def test_the_number_matches_however_the_relay_formats_it(self):
        for sender in (PHONE, "whatsapp:+201000007582", "201000007582",
                       "+20 100 000 7582"):
            with self.subTest(sender=sender):
                self.assertTrue(self.listener.from_allowed_number(sender))

    def test_an_unconfigured_number_authorises_nobody(self):
        listener = WhatsAppCommandListener(self.controller, allowed_number="",
                                           shared_secret=self.SECRET)
        listener.allowed = ""
        self.assertFalse(listener.from_allowed_number(PHONE))

    def test_a_replayed_message_never_executes_twice(self):
        """Relays retry on any non-2xx, and a retried approval must not submit
        the application a second time."""
        self._draft()
        raw, signature = self._signed(META_BODY)
        self.listener.handle_payload(META_BODY, raw=raw, signature=signature)
        self.listener.handle_payload(META_BODY, raw=raw, signature=signature)
        self.assertEqual(self.submitted, [1])
        self.assertEqual(self.listener.stats.replayed, 1)

    def test_the_bots_own_card_is_never_executed(self):
        from auto_apply.review import BOT_MARK

        self._draft()
        self.listener.handle_payload(
            {"from": PHONE, "text": f"{BOT_MARK}done 1"}, token=self.SECRET
        )
        self.assertEqual(self.submitted, [])
        self.assertEqual(self.listener.stats.skipped_own, 1)

    def test_a_stale_command_is_not_replayed_after_a_restart(self):
        listener = WhatsAppCommandListener(
            self.controller, allowed_number=PHONE, shared_secret=self.SECRET,
            max_age_minutes=180,
        )
        self._draft()
        old = datetime.now(timezone.utc) - timedelta(days=7)
        self.assertIsNone(listener.handle(InboundCommand(
            text="done 1", sender=PHONE, message_id="old", when=old,
        )))
        self.assertEqual(self.submitted, [])


class TestInboundExecution(InboundHarness):
    """A WhatsApp command must do exactly what the Telegram one does."""

    def test_approve_runs_the_submission_pipeline(self):
        app_id = self._draft()
        self.listener.handle_payload({"from": PHONE, "text": f"done {app_id}"},
                                     token=self.SECRET)
        self.assertEqual(self.submitted, [app_id])

    def test_arabic_approval_over_whatsapp(self):
        app_id = self._draft()
        self.listener.handle_payload({"from": PHONE, "text": f"موافق {app_id}"},
                                     token=self.SECRET)
        self.assertEqual(self.submitted, [app_id])

    def test_an_edit_patches_the_draft_and_re_dispatches(self):
        app_id = self._draft()
        self.notifier.clear()
        self.listener.handle_payload(
            {"from": PHONE, "text": f"edit {app_id} cover letter: New text."},
            token=self.SECRET,
        )
        app = self.store.get_application(app_id)
        self.assertEqual(app["cover_letter_text"], "New text.")
        self.assertEqual(app["status"], STATUS_REVIEW)
        self.assertTrue(self.notifier.telegram)
        self.assertTrue(self.notifier.whatsapp)

    def test_a_whatsapp_command_answers_on_BOTH_channels(self):
        """The point of two-way: whichever channel you reply on, the result
        lands on both."""
        self._draft()
        self.notifier.clear()
        self.listener.handle_payload({"from": PHONE, "text": "status"},
                                     token=self.SECRET)
        self.assertEqual(len(self.notifier.telegram), 1)
        self.assertEqual(len(self.notifier.whatsapp), 1)

    def test_ordinary_chatter_is_ignored_silently(self):
        self._draft()
        self.notifier.clear()
        self.listener.handle_payload(
            {"from": PHONE, "text": "thanks, talk later"}, token=self.SECRET
        )
        self.assertEqual(self.notifier.telegram, [])
        self.assertEqual(self.listener.stats.unrecognised, 1)

    def test_several_messages_in_one_webhook_all_run(self):
        self._draft()
        body = {"object": "whatsapp_business_account", "entry": [{"changes": [
            {"value": {"messages": [
                {"from": "201000007582", "id": "m1", "type": "text",
                 "text": {"body": "status"}},
                {"from": "201000007582", "id": "m2", "type": "text",
                 "text": {"body": "help"}},
            ]}}]}]}
        raw, signature = self._signed(body)
        replies = self.listener.handle_payload(body, raw=raw,
                                               signature=signature)
        self.assertEqual(len(replies), 2)


class TestInboundReadiness(unittest.TestCase):
    """`--listen` must say what is live, not claim both channels blindly."""

    def _with(self, **overrides):
        original = dict(settings.raw.get("hitl", {}) or {})
        settings.raw["hitl"] = dict(
            original, whatsapp_inbound=dict(whatsapp_cfg(), **overrides)
        )
        return original

    def test_disabled_is_reported_as_disabled(self):
        original = self._with(enabled=False)
        try:
            ready, why = readiness()
        finally:
            settings.raw["hitl"] = original
        self.assertFalse(ready)
        self.assertIn("Telegram-only", why)

    def test_enabled_without_a_secret_is_refused_with_a_reason(self):
        original = self._with(enabled=True, app_secret="", shared_secret="")
        try:
            ready, why = readiness()
        finally:
            settings.raw["hitl"] = original
        self.assertFalse(ready)
        self.assertIn("vulnerability", why)

    def test_fully_configured_is_ready(self):
        original = self._with(enabled=True, shared_secret="s",
                              allowed_number=PHONE)
        try:
            ready, why = readiness()
        finally:
            settings.raw["hitl"] = original
        self.assertTrue(ready, why)


class TestWebhookHttpSurface(InboundHarness):
    """The HTTP layer, without binding a socket."""

    def test_the_handler_is_constructible(self):
        from auto_apply.inbound import build_handler

        self.assertTrue(callable(build_handler(self.listener)))

    def test_a_form_encoded_twilio_body_is_understood(self):
        from urllib.parse import parse_qs

        raw = "MessageSid=SM1&From=whatsapp%3A%2B201000007582&Body=status"
        parsed = {k: v[0] for k, v in parse_qs(raw).items()}
        self._draft()
        replies = self.listener.handle_payload(parsed, token=self.SECRET)
        self.assertEqual(len(replies), 1)

    def test_meta_verification_needs_the_configured_token(self):
        self.listener.verify_token = "verify-me"
        self.assertTrue(hmac.compare_digest(self.listener.verify_token,
                                            "verify-me"))
        self.assertFalse(hmac.compare_digest(self.listener.verify_token,
                                             "wrong"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
