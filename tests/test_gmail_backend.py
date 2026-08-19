"""
Gmail API backend: message parsing and backend selection.

No network. The Gmail API returns a nested MIME tree with base64url payloads,
and getting that wrong means recruiter mail silently arrives with an empty body
-- which then classifies as "not job related" and is dropped. So the parsing is
tested against realistic payload shapes rather than trusted.

Run:  python tests/test_gmail_backend.py
"""

from __future__ import annotations

import base64
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("CALLMEBOT_APIKEY", "test-apikey")
os.environ.setdefault("WHATSAPP_PHONE", "+201234567890")
os.environ.setdefault("CV_TEXT", "VoIP engineer with SIP and Issabel PBX. " * 6)
os.environ.setdefault("DRY_RUN", "true")

from auto_apply.email_listener import (                    # noqa: E402
    GmailApiBackend, ImapBackend, MailBackend,
)


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


class TestGmailPayloadParsing(unittest.TestCase):
    def test_header_lookup_is_case_insensitive(self):
        payload = {"headers": [
            {"name": "From", "value": "hr@etisalat.ae"},
            {"name": "SUBJECT", "value": "Interview invitation"},
        ]}
        self.assertEqual(GmailApiBackend._header(payload, "from"), "hr@etisalat.ae")
        self.assertEqual(GmailApiBackend._header(payload, "Subject"),
                         "Interview invitation")

    def test_missing_header_is_empty_not_an_error(self):
        self.assertEqual(GmailApiBackend._header({"headers": []}, "From"), "")
        self.assertEqual(GmailApiBackend._header({}, "From"), "")

    def test_plain_text_body_is_decoded(self):
        payload = {"mimeType": "text/plain",
                   "body": {"data": b64("Please join the interview.")}}
        self.assertIn("interview", GmailApiBackend._extract_body(payload))

    def test_plain_text_is_preferred_over_html(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html",
                 "body": {"data": b64("<p>HTML version</p>")}},
                {"mimeType": "text/plain",
                 "body": {"data": b64("PLAIN version")}},
            ],
        }
        body = GmailApiBackend._extract_body(payload)
        self.assertIn("PLAIN version", body)
        self.assertNotIn("HTML version", body)

    def test_html_only_mail_is_de_tagged(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [{"mimeType": "text/html",
                       "body": {"data": b64("<div><b>Interview</b> at 2pm</div>")}}],
        }
        body = GmailApiBackend._extract_body(payload)
        self.assertIn("Interview", body)
        self.assertNotIn("<b>", body)

    def test_nested_multipart_is_walked(self):
        """Real recruiter mail nests: mixed > alternative > plain."""
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "application/pdf", "body": {"attachmentId": "x"}},
                {"mimeType": "multipart/alternative", "parts": [
                    {"mimeType": "text/plain",
                     "body": {"data": b64("Buried three levels down")}},
                ]},
            ],
        }
        self.assertIn("Buried three levels down",
                      GmailApiBackend._extract_body(payload))

    def test_arabic_body_survives_decoding(self):
        payload = {"mimeType": "text/plain",
                   "body": {"data": b64("دعوة لإجراء مقابلة شخصية")}}
        self.assertIn("مقابلة", GmailApiBackend._extract_body(payload))

    def test_undecodable_payload_does_not_raise(self):
        payload = {"mimeType": "text/plain", "body": {"data": "!!!not-base64!!!"}}
        self.assertEqual(GmailApiBackend._extract_body(payload), "")

    def test_empty_payload_is_empty_string(self):
        self.assertEqual(GmailApiBackend._extract_body({}), "")

    def test_body_is_length_capped(self):
        payload = {"mimeType": "text/plain", "body": {"data": b64("x" * 20000)}}
        self.assertLessEqual(len(GmailApiBackend._extract_body(payload)), 8000)


class TestBackendSelection(unittest.TestCase):
    """Gmail API when authorised, IMAP when not, a clear error when neither."""

    def test_both_backends_satisfy_the_interface(self):
        for cls in (GmailApiBackend, ImapBackend):
            self.assertTrue(issubclass(cls, MailBackend))
            for method in ("fetch_unread", "mark_seen", "account"):
                self.assertTrue(callable(getattr(cls, method)))

    def test_gmail_api_is_preferred_when_a_token_exists(self):
        import auto_apply.email_listener as listener
        from auto_apply import gmail_oauth

        real_configured = gmail_oauth.is_configured
        real_backend = listener.GmailApiBackend
        gmail_oauth.is_configured = lambda: True
        listener.GmailApiBackend = lambda: type(
            "Fake", (MailBackend,), {"name": "gmail-api"}
        )()
        try:
            self.assertEqual(listener.select_backend().name, "gmail-api")
        finally:
            gmail_oauth.is_configured = real_configured
            listener.GmailApiBackend = real_backend

    def test_falls_back_to_imap_when_the_api_is_unusable(self):
        """A broken token must not block the legacy route.

        The IMAP credentials are set HERE rather than inherited from the
        environment. This test used to pass only on a machine whose .env still
        held a dead App Password; with a clean environment `select_backend()`
        had nothing to fall back TO and the test failed for a reason that had
        nothing to do with what it claims to check.
        """
        import auto_apply.email_listener as listener
        from auto_apply import gmail_oauth
        from config import settings

        real_configured = gmail_oauth.is_configured
        real_backend = listener.GmailApiBackend
        real_email = settings.job_email
        real_pw = settings.job_email_password

        def explode():
            raise RuntimeError("token revoked")

        gmail_oauth.is_configured = lambda: True
        listener.GmailApiBackend = explode
        settings.job_email = "legacy@example.invalid"
        settings.job_email_password = "abcd efgh ijkl mnop"
        try:
            backend = listener.select_backend()
            self.assertEqual(backend.name, "imap")
        finally:
            gmail_oauth.is_configured = real_configured
            listener.GmailApiBackend = real_backend
            settings.job_email = real_email
            settings.job_email_password = real_pw

    def test_no_credentials_at_all_names_both_routes(self):
        import auto_apply.email_listener as listener
        from auto_apply import gmail_oauth
        from config import settings

        real_configured = gmail_oauth.is_configured
        real_pw = settings.job_email_password
        gmail_oauth.is_configured = lambda: False
        settings.job_email_password = ""
        try:
            with self.assertRaises(RuntimeError) as ctx:
                listener.select_backend()
            message = str(ctx.exception)
            self.assertIn("auth_gmail.py", message)
            self.assertIn("JOB_EMAIL_APP_PASSWORD", message)
        finally:
            gmail_oauth.is_configured = real_configured
            settings.job_email_password = real_pw


class TestOAuthArtefactsAreProtected(unittest.TestCase):
    """The token is mailbox access; it must be unpublishable."""

    def test_token_and_client_secret_are_git_ignored(self):
        ignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(
            encoding="utf-8"
        )
        self.assertIn("secrets/gmail_token.json", ignore)
        self.assertIn("secrets/gmail_client_secret.json", ignore)

    def test_scope_is_limited_to_gmail(self):
        from auto_apply.gmail_oauth import SCOPES

        self.assertEqual(len(SCOPES), 1)
        self.assertTrue(SCOPES[0].startswith("https://www.googleapis.com/auth/gmail"))

    def test_no_client_secret_is_embedded_in_the_source(self):
        """A shipped OAuth client secret would be a credential in a public repo."""
        source = (Path(__file__).resolve().parent.parent
                  / "auto_apply" / "gmail_oauth.py").read_text(encoding="utf-8")
        self.assertNotIn("client_secret:", source)
        self.assertNotIn("GOCSPX-", source, "a real Google client secret is embedded")

    def test_non_interactive_load_never_opens_a_browser(self):
        """A scheduled run must fail fast, not hang on a consent screen.

        The token path is redirected to an empty temp directory instead of
        skipping when a real token is present. Self-skipping meant this ran
        only on machines where Gmail was NOT set up -- i.e. never on the
        machine anyone was actually developing on, which is precisely where a
        blocking consent screen would be introduced.
        """
        import tempfile

        from auto_apply import gmail_oauth
        from auto_apply.gmail_oauth import GmailAuthError

        real_token = gmail_oauth.TOKEN_PATH
        gmail_oauth.TOKEN_PATH = Path(tempfile.mkdtemp()) / "absent_token.json"
        try:
            with self.assertRaises(GmailAuthError) as ctx:
                gmail_oauth.load_credentials(interactive=False)
            self.assertIn("auth_gmail.py", str(ctx.exception))
        finally:
            gmail_oauth.TOKEN_PATH = real_token


if __name__ == "__main__":
    unittest.main(verbosity=2)
