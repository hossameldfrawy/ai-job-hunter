"""
Gmail over OAuth2: token load, silent refresh, the IMAP fallback, and how an
inbound message is triaged.

WHY OAUTH AND NOT AN APP PASSWORD
---------------------------------
Google no longer issues App Passwords on newer accounts -- the setting is simply
absent -- so IMAP-with-a-password is not a route in at all on the mailbox this
bot is built for. OAuth2 is, and it is strictly better: the grant is scoped to
Gmail alone and can be revoked without changing any password.

THE PROPERTY THAT MATTERS MOST HERE
-----------------------------------
`load_credentials(interactive=False)` must NEVER open a browser. A scheduled run
that hits a consent screen does not fail -- it HANGS, silently, until the job
times out, and the inbox stops being watched with no error anyone sees. So the
non-interactive path either returns working credentials or raises, and that is
asserted directly rather than assumed.

Nothing here reads the real `secrets/gmail_token.json`: every test redirects the
module's paths at a temporary directory first. Reading it would be a privacy
leak in a test suite, and writing it would destroy the user's live grant.

Run:  python -m pytest tests/test_gmail_oauth.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_apply import gmail_oauth                              # noqa: E402
from auto_apply.email_listener import (                         # noqa: E402
    EmailMonitor, GmailApiBackend, ImapBackend, InboxMessage, _MEETING_HOSTS,
    select_backend,
)
from auto_apply.gmail_oauth import (                            # noqa: E402
    GmailAuthError, SCOPES, SETUP_HELP, is_configured,
    libraries_available,
)

HAVE_GOOGLE = libraries_available()


class FakeCredentials:
    """Stands in for google.oauth2.credentials.Credentials."""

    def __init__(self, valid=True, expired=False, refresh_token="rt",
                 refresh_raises=None):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.refresh_raises = refresh_raises
        self.refreshed = False

    def refresh(self, _request):
        self.refreshed = True
        if self.refresh_raises:
            raise self.refresh_raises
        self.valid = True
        self.expired = False

    def to_json(self):
        return json.dumps({"token": "fake", "refresh_token": self.refresh_token})


@contextmanager
def sandboxed_paths():
    """Point the module at a temp directory. The real token is never touched."""
    tmp = Path(tempfile.mkdtemp())
    original_token = gmail_oauth.TOKEN_PATH
    original_secret = gmail_oauth.CLIENT_SECRET_PATH
    original_dir = gmail_oauth.SECRETS_DIR
    gmail_oauth.TOKEN_PATH = tmp / "gmail_token.json"
    gmail_oauth.CLIENT_SECRET_PATH = tmp / "gmail_client_secret.json"
    gmail_oauth.SECRETS_DIR = tmp
    try:
        yield tmp
    finally:
        gmail_oauth.TOKEN_PATH = original_token
        gmail_oauth.CLIENT_SECRET_PATH = original_secret
        gmail_oauth.SECRETS_DIR = original_dir


@contextmanager
def stored_credentials(creds, load_error=None):
    """Make `Credentials.from_authorized_user_file` return `creds`."""
    from google.oauth2 import credentials as credentials_mod

    original = credentials_mod.Credentials.from_authorized_user_file

    def fake(path, scopes):
        if load_error:
            raise load_error
        return creds

    credentials_mod.Credentials.from_authorized_user_file = staticmethod(fake)
    try:
        yield
    finally:
        credentials_mod.Credentials.from_authorized_user_file = original


# ---------------------------------------------------------------------------
class TestConfigurationDetection(unittest.TestCase):
    def test_is_configured_needs_both_a_token_and_the_libraries(self):
        with sandboxed_paths() as tmp:
            self.assertFalse(is_configured(), "no token, yet reported ready")
            (tmp / "gmail_token.json").write_text("{}", encoding="utf-8")
            self.assertEqual(is_configured(), libraries_available())

    def test_the_scope_is_limited_to_gmail(self):
        """`modify` is the narrowest scope that still lets the monitor mark job
        mail read. It grants nothing outside Gmail."""
        self.assertEqual(SCOPES, ["https://www.googleapis.com/auth/gmail.modify"])
        for scope in SCOPES:
            self.assertIn("gmail", scope)
            self.assertNotIn("drive", scope)
            self.assertNotIn("contacts", scope)

    def test_no_client_secret_is_embedded_in_the_source(self):
        source = Path(gmail_oauth.__file__).read_text(encoding="utf-8")
        self.assertNotIn("client_secret\":", source)
        self.assertNotIn(".apps.googleusercontent.com", source)

    def test_the_setup_help_names_every_step(self):
        for needle in ("console.cloud.google.com", "Gmail API",
                       "OAuth client ID", "auth_gmail.py"):
            with self.subTest(needle=needle):
                self.assertIn(needle, SETUP_HELP)

    def test_the_token_and_client_secret_are_git_ignored(self):
        root = Path(__file__).resolve().parent.parent
        ignored = (root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("secrets/", ignored)


@unittest.skipUnless(HAVE_GOOGLE, "google-auth libraries are not installed")
class TestCredentialLoading(unittest.TestCase):
    def test_a_valid_token_is_returned_without_touching_the_network(self):
        creds = FakeCredentials(valid=True)
        with sandboxed_paths() as tmp:
            (tmp / "gmail_token.json").write_text("{}", encoding="utf-8")
            with stored_credentials(creds):
                self.assertIs(gmail_oauth.load_credentials(), creds)
        self.assertFalse(creds.refreshed, "a valid token was needlessly refreshed")

    def test_an_expired_token_is_refreshed_silently_and_written_back(self):
        """This is the path every scheduled run takes; it must not prompt."""
        creds = FakeCredentials(valid=False, expired=True, refresh_token="rt")
        with sandboxed_paths() as tmp:
            token = tmp / "gmail_token.json"
            token.write_text("{}", encoding="utf-8")
            with stored_credentials(creds):
                self.assertIs(gmail_oauth.load_credentials(), creds)
            self.assertTrue(creds.refreshed)
            self.assertIn("refresh_token", token.read_text(encoding="utf-8"))

    def test_a_failed_refresh_raises_rather_than_prompting(self):
        creds = FakeCredentials(valid=False, expired=True,
                                refresh_raises=RuntimeError("invalid_grant"))
        with sandboxed_paths() as tmp:
            (tmp / "gmail_token.json").write_text("{}", encoding="utf-8")
            with stored_credentials(creds):
                with self.assertRaises(GmailAuthError) as ctx:
                    gmail_oauth.load_credentials(interactive=False)
        self.assertIn("auth_gmail.py", str(ctx.exception))

    def test_a_token_with_no_refresh_token_cannot_renew_itself(self):
        creds = FakeCredentials(valid=False, expired=True, refresh_token=None)
        with sandboxed_paths() as tmp:
            (tmp / "gmail_token.json").write_text("{}", encoding="utf-8")
            with stored_credentials(creds):
                with self.assertRaises(GmailAuthError):
                    gmail_oauth.load_credentials(interactive=False)

    def test_an_unreadable_token_is_reported_not_swallowed(self):
        with sandboxed_paths() as tmp:
            (tmp / "gmail_token.json").write_text("{not json", encoding="utf-8")
            with stored_credentials(None, load_error=ValueError("corrupt")):
                with self.assertRaises(GmailAuthError):
                    gmail_oauth.load_credentials(interactive=False)

    def test_no_token_at_all_raises_without_opening_a_browser(self):
        """A scheduled run that hits a consent screen HANGS until timeout."""
        opened = []

        with sandboxed_paths():
            from google_auth_oauthlib import flow as flow_mod

            original = flow_mod.InstalledAppFlow.from_client_secrets_file
            flow_mod.InstalledAppFlow.from_client_secrets_file = staticmethod(
                lambda *a, **k: opened.append(1)
            )
            try:
                with self.assertRaises(GmailAuthError) as ctx:
                    gmail_oauth.load_credentials(interactive=False)
            finally:
                flow_mod.InstalledAppFlow.from_client_secrets_file = original
        self.assertEqual(opened, [], "a browser flow was started headlessly")
        self.assertIn("not running interactively", str(ctx.exception))

    def test_interactive_without_a_client_secret_prints_the_setup_guide(self):
        with sandboxed_paths():
            with self.assertRaises(GmailAuthError) as ctx:
                gmail_oauth.load_credentials(interactive=True)
        self.assertIn("console.cloud.google.com", str(ctx.exception))

    def test_saving_a_token_creates_a_gitignored_secrets_directory(self):
        with sandboxed_paths() as tmp:
            gmail_oauth._save(FakeCredentials())
            self.assertTrue((tmp / "gmail_token.json").exists())
            self.assertEqual((tmp / ".gitignore").read_text(encoding="utf-8"),
                             "*\n")

    def test_revoke_deletes_only_the_local_token(self):
        with sandboxed_paths() as tmp:
            token = tmp / "gmail_token.json"
            self.assertFalse(gmail_oauth.revoke(), "nothing to revoke yet")
            token.write_text("{}", encoding="utf-8")
            self.assertTrue(gmail_oauth.revoke())
            self.assertFalse(token.exists())

    def test_missing_libraries_explain_the_install_command(self):
        original = gmail_oauth.libraries_available
        gmail_oauth.libraries_available = lambda: False
        try:
            with self.assertRaises(GmailAuthError) as ctx:
                gmail_oauth.load_credentials()
        finally:
            gmail_oauth.libraries_available = original
        self.assertIn("pip install", str(ctx.exception))


# ---------------------------------------------------------------------------
class TestBackendSelection(unittest.TestCase):
    """Gmail API first, IMAP second, and a message naming both if neither."""

    def test_both_backends_satisfy_one_interface(self):
        from auto_apply.email_listener import MailBackend

        for backend in (GmailApiBackend, ImapBackend):
            with self.subTest(backend=backend.__name__):
                self.assertTrue(issubclass(backend, MailBackend))
                for method in ("fetch_unread", "mark_seen"):
                    self.assertTrue(callable(getattr(backend, method)))

    def test_the_api_is_preferred_when_a_token_exists(self):
        built = []

        original_configured = gmail_oauth.is_configured
        original_init = GmailApiBackend.__init__
        gmail_oauth.is_configured = lambda: True

        def fake_init(self):
            built.append(1)
            self.name = "gmail_api"

        GmailApiBackend.__init__ = fake_init
        try:
            backend = select_backend()
        finally:
            gmail_oauth.is_configured = original_configured
            GmailApiBackend.__init__ = original_init
        self.assertIsInstance(backend, GmailApiBackend)
        self.assertEqual(built, [1])

    def test_an_unusable_api_falls_back_to_imap(self):
        from config import settings

        original_configured = gmail_oauth.is_configured
        original_init = GmailApiBackend.__init__
        original_email = settings.job_email
        original_password = settings.job_email_password

        gmail_oauth.is_configured = lambda: True

        def exploding_init(self):
            raise RuntimeError("token revoked")

        GmailApiBackend.__init__ = exploding_init
        settings.job_email = "fallback@example.com"
        settings.job_email_password = "app password"
        try:
            backend = select_backend()
        finally:
            gmail_oauth.is_configured = original_configured
            GmailApiBackend.__init__ = original_init
            settings.job_email = original_email
            settings.job_email_password = original_password
        self.assertIsInstance(backend, ImapBackend)

    def test_no_route_at_all_names_both_options(self):
        from config import settings

        original_configured = gmail_oauth.is_configured
        original_email = settings.job_email
        gmail_oauth.is_configured = lambda: False
        settings.job_email = ""
        try:
            with self.assertRaises(RuntimeError) as ctx:
                select_backend()
        finally:
            gmail_oauth.is_configured = original_configured
            settings.job_email = original_email
        message = str(ctx.exception)
        self.assertIn("auth_gmail.py", message)
        self.assertIn("JOB_EMAIL_APP_PASSWORD", message)

    def test_email_monitor_ready_accepts_either_route(self):
        """It used to demand an App Password unconditionally, which meant
        `--inbox` refused to start on the very setup OAuth exists to support."""
        from config import settings

        original_configured = gmail_oauth.is_configured
        original_email = settings.job_email
        original_password = settings.job_email_password
        try:
            settings.job_email = ""
            settings.job_email_password = ""
            gmail_oauth.is_configured = lambda: True
            self.assertTrue(settings.email_monitor_ready)

            gmail_oauth.is_configured = lambda: False
            self.assertFalse(settings.email_monitor_ready)

            settings.job_email = "a@b.c"
            settings.job_email_password = "pw"
            self.assertTrue(settings.email_monitor_ready)
        finally:
            gmail_oauth.is_configured = original_configured
            settings.job_email = original_email
            settings.job_email_password = original_password


# ---------------------------------------------------------------------------
class TestInboxTriage(unittest.TestCase):
    """The local gate, the link extraction, and the four intents."""

    @staticmethod
    def _message(subject="Interview invitation", body="Please join us.",
                 sender="hr@etisalat.ae", message_id="<m1@x>"):
        return InboxMessage(message_id=message_id, uid="1", sender=sender,
                            subject=subject, body=body, received=None)

    def test_job_mail_clears_the_cheap_gate(self):
        for subject in ("Interview invitation", "Your application to Etisalat",
                        "Assessment link", "We received your CV",
                        "دعوة مقابلة عمل", "بخصوص تقديمك على وظيفة"):
            with self.subTest(subject=subject):
                self.assertTrue(self._message(subject=subject).looks_job_related())

    def test_personal_mail_never_reaches_the_model(self):
        """The gate protects the Gemini quota AND the user's privacy."""
        for subject, body in (
            ("Your Netflix bill", "Your monthly statement is ready."),
            ("Happy birthday!", "Have a great day."),
            ("طلبك من سوق كوم", "تم شحن الطلب"),
        ):
            with self.subTest(subject=subject):
                self.assertFalse(
                    self._message(subject=subject, body=body).looks_job_related()
                )

    def test_meeting_links_are_extracted_and_marketing_links_ignored(self):
        body = ("Join at https://zoom.us/j/123456 or read more at "
                "https://etisalat.ae/careers and unsubscribe at "
                "https://mailchimp.com/x")
        links = self._message(body=body).candidate_links()
        self.assertIn("https://zoom.us/j/123456", links)
        self.assertNotIn("https://mailchimp.com/x", links)

    def test_every_assessment_platform_counts_as_a_meeting_link(self):
        for host in _MEETING_HOSTS:
            with self.subTest(host=host):
                links = self._message(
                    body=f"Start here: https://{host}/test/1"
                ).candidate_links()
                self.assertTrue(links, host)

    def test_a_message_with_no_links_is_empty_not_an_error(self):
        self.assertEqual(self._message(body="We will be in touch.")
                         .candidate_links(), [])

    def test_each_intent_gets_its_own_headline(self):
        cases = {
            "interview": "INTERVIEW INVITATION RECEIVED!",
            "assessment": "ASSESSMENT / TEST RECEIVED",
            "rejection": "Application closed",
            "acknowledgment": "Application acknowledged",
            "recruiter_outreach": "RECRUITER REACHED OUT",
            "other": "Job-related email",
        }
        for classification, headline in cases.items():
            with self.subTest(classification=classification):
                alert = EmailMonitor.format_alert(
                    {"classification": classification, "company": "Etisalat"},
                    None,
                )
                self.assertIn(headline, alert)

    def test_a_rejection_is_never_dressed_up_as_good_news(self):
        alert = EmailMonitor.format_alert(
            {"classification": "rejection", "company": "Etisalat",
             "summary": "They went with another candidate."}, None,
        )
        self.assertNotIn("!", alert.splitlines()[0])
        self.assertNotIn("🎉", alert)

    def test_an_interview_alert_carries_the_time_the_link_and_the_application(self):
        alert = EmailMonitor.format_alert(
            {
                "classification": "interview", "company": "Etisalat",
                "role": "VoIP Engineer",
                "meeting_datetime": "Tuesday 26 Aug, 11:00 GST",
                "meeting_link": "https://meet.google.com/abc-defg-hij",
                "action_required": "Confirm attendance by Monday",
                "summary": "First-round technical interview.",
            },
            {"id": 7, "role": "VoIP Engineer", "platform": "tanqeeb",
             "submitted_at": "2026-08-19T10:15:00+00:00"},
        )
        for needle in ("Etisalat", "VoIP Engineer", "11:00 GST",
                       "meet.google.com", "Confirm attendance", "#7"):
            with self.subTest(needle=needle):
                self.assertIn(needle, alert)

    def test_an_unlinked_alert_says_so_rather_than_implying_a_match(self):
        alert = EmailMonitor.format_alert(
            {"classification": "interview", "company": "Unknown Co"}, None
        )
        self.assertIn("not linked to a tracked application", alert)

    def test_missing_fields_render_as_unknown_not_as_none(self):
        alert = EmailMonitor.format_alert({"classification": "interview"}, None)
        self.assertNotIn("None", alert)
        self.assertIn("(unknown)", alert)


if __name__ == "__main__":
    unittest.main(verbosity=2)
