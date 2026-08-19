"""
Inbound interview & status monitor (IMAP).

Watches the job-hunt mailbox, classifies recruiter mail with Gemini, and pushes
interview invitations to Telegram with the meeting time and link already pulled
out -- so an invitation buried in a Tuesday inbox does not get noticed on
Thursday.

Two properties this is careful about, because it reads a real personal mailbox:

  * NOTHING IS MARKED READ UNLESS IT IS JOB MAIL. Fetches use BODY.PEEK, which
    leaves \\Seen untouched. Only a message Gemini classifies as job-related is
    flagged, and only when `mark_seen_when_classified` is on. A personal email
    that happens to sit in the same inbox is read and forgotten, not touched.
  * A CHEAP LOCAL FILTER RUNS FIRST. Gemini never sees a message unless it looks
    plausibly job-related, which keeps both the quota and the privacy exposure
    down.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import re
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from typing import Any

from config import settings
from vault import SecureStore

log = logging.getLogger(__name__)

CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "is_job_related": {"type": "BOOLEAN"},
        "classification": {
            "type": "STRING",
            "description": (
                "One of: interview, assessment, rejection, acknowledgment, "
                "recruiter_outreach, other."
            ),
        },
        "company": {"type": "STRING"},
        "role": {"type": "STRING"},
        "meeting_datetime": {
            "type": "STRING",
            "description": (
                "The interview or deadline time exactly as stated, including "
                "timezone if given. Empty string if none."
            ),
        },
        "meeting_link": {
            "type": "STRING",
            "description": (
                "Video-call or assessment URL (Zoom/Meet/Teams/HackerRank...). "
                "Empty string if none."
            ),
        },
        "action_required": {
            "type": "STRING",
            "description": "What the candidate must DO next, in one short line.",
        },
        "summary": {
            "type": "STRING",
            "description": "Two sentences maximum, factual.",
        },
        "urgency": {
            "type": "STRING",
            "description": "high, normal or low.",
        },
    },
    "required": [
        "is_job_related", "classification", "company", "role",
        "meeting_datetime", "meeting_link", "action_required", "summary",
        "urgency",
    ],
}

CLASSIFY_INSTRUCTION = """\
You are triaging one email in a job-seeker's inbox.

Decide whether it relates to a job application, and if so what it is:
  interview          -- an invitation to interview, or a scheduling request
  assessment         -- a test, coding task or take-home with a deadline
  rejection          -- an unsuccessful outcome
  acknowledgment     -- "we received your application", automated receipts
  recruiter_outreach -- a recruiter approaching about a role, unprompted
  other              -- job-adjacent but none of the above

Set is_job_related=false for newsletters, marketing, job-board digests and
anything personal. A job ALERT digest is not an application update.

Extract the meeting time and the joining link verbatim when present. Never
invent a time. If the message only says "we will be in touch", there is no
meeting time.
"""

# Cheap gate: Gemini never sees a message that fails this.
_JOB_HINTS = re.compile(
    r"(interview|application|applied|position|vacancy|role|candidate|recruit|"
    r"hiring|shortlist|assessment|screening|offer|cv|resume|hr\b|talent|"
    r"مقابلة|وظيف|تقديم|السيرة الذاتية|توظيف|اختبار)",
    re.I,
)
_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)
_MEETING_HOSTS = (
    "zoom.us", "meet.google", "teams.microsoft", "teams.live", "webex",
    "whereby.com", "calendly.com", "hackerrank", "codility", "testgorilla",
    "karat.com", "gomeeting",
)


@dataclass(slots=True)
class InboxMessage:
    uid: str
    message_id: str
    sender: str
    subject: str
    body: str
    received: datetime | None

    def looks_job_related(self) -> bool:
        return bool(_JOB_HINTS.search(f"{self.subject}\n{self.body[:2500]}"))

    def candidate_links(self) -> list[str]:
        found = _URL_RE.findall(self.body or "")
        return [u for u in found if any(h in u.lower() for h in _MEETING_HOSTS)]


# Characters that survive a copy-paste from a web page and look identical to a
# normal space: non-breaking space, narrow NBSP, zero-width space/joiner.
_INVISIBLE = re.compile(r"[\s  ​‌‍﻿]+")


def _password_variants(raw: str) -> list[str]:
    """Every surface form of an app password worth trying, most likely first.

    Google renders app passwords as four spaced groups, and people paste them
    with the spaces, without them, or with an invisible NBSP that Gmail rejects
    while looking identical on screen. Stripping is done against a character
    class rather than `.strip()` so those invisibles are actually removed.
    """
    if not raw:
        return []
    squashed = _INVISIBLE.sub("", raw)
    spaced = " ".join(squashed[i:i + 4] for i in range(0, len(squashed), 4))
    out: list[str] = []
    for value in (squashed, spaced, raw.strip(), raw):
        if value and value not in out:
            out.append(value)
    return out


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _body_text(msg: email.message.Message) -> str:
    """Prefer text/plain; fall back to de-tagged HTML."""
    plain, html_part = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition:
                continue
            try:
                payload = part.get_payload(decode=True) or b""
                text = payload.decode(part.get_content_charset() or "utf-8",
                                      errors="replace")
            except Exception:
                continue
            if part.get_content_type() == "text/plain" and not plain:
                plain = text
            elif part.get_content_type() == "text/html" and not html_part:
                html_part = text
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            plain = payload.decode(msg.get_content_charset() or "utf-8",
                                   errors="replace")
        except Exception:
            plain = ""

    text = plain or re.sub(r"<[^>]+>", " ", html_part)
    return re.sub(r"[ \t\xa0]+", " ", text).strip()[:8000]


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
# Two ways into the same mailbox, behind one interface. Gmail API is preferred
# because Google no longer issues App Passwords on newer accounts -- the setting
# is simply absent -- so IMAP is not an option there at all. IMAP is kept for
# older accounts that still have a working password, and for any non-Gmail host.


class MailBackend:
    """Fetch unread mail and optionally flag it read."""

    name = "base"

    def fetch_unread(self, lookback_days: int, limit: int) -> list[InboxMessage]:
        raise NotImplementedError

    def mark_seen(self, message: InboxMessage) -> None:
        raise NotImplementedError

    def account(self) -> str:
        return ""


class GmailApiBackend(MailBackend):
    """Gmail API over OAuth2. The supported path on modern accounts."""

    name = "gmail-api"

    def __init__(self) -> None:
        from auto_apply.gmail_oauth import gmail_service

        self._service = gmail_service(interactive=False)

    def account(self) -> str:
        try:
            profile = self._service.users().getProfile(userId="me").execute()
            return str(profile.get("emailAddress", ""))
        except Exception:
            return ""

    @staticmethod
    def _header(payload: dict[str, Any], name: str) -> str:
        for h in payload.get("headers", []) or []:
            if str(h.get("name", "")).lower() == name.lower():
                return str(h.get("value", ""))
        return ""

    @staticmethod
    def _decode_part(data: str) -> str:
        import base64

        try:
            return base64.urlsafe_b64decode(data.encode()).decode(
                "utf-8", errors="replace"
            )
        except Exception:
            return ""

    @classmethod
    def _extract_body(cls, payload: dict[str, Any]) -> str:
        """Walk the MIME tree, preferring text/plain over de-tagged HTML."""
        plain, html_body = "", ""
        stack = [payload]
        while stack:
            part = stack.pop()
            mime = str(part.get("mimeType", ""))
            body = part.get("body", {}) or {}
            data = body.get("data")
            if data:
                if mime == "text/plain" and not plain:
                    plain = cls._decode_part(data)
                elif mime == "text/html" and not html_body:
                    html_body = cls._decode_part(data)
            stack.extend(part.get("parts", []) or [])

        text = plain or re.sub(r"<[^>]+>", " ", html_body)
        return re.sub(r"[ \t\xa0]+", " ", text).strip()[:8000]

    def fetch_unread(self, lookback_days: int, limit: int) -> list[InboxMessage]:
        query = f"is:unread newer_than:{max(1, lookback_days)}d"
        listing = self._service.users().messages().list(
            userId="me", q=query, maxResults=max(1, limit),
        ).execute()

        out: list[InboxMessage] = []
        for stub in listing.get("messages", []) or []:
            # `get` READS a message; it does NOT mark it read. Only an explicit
            # modify() removing the UNREAD label does that, which is what makes
            # scanning a personal inbox safe here.
            msg = self._service.users().messages().get(
                userId="me", id=stub["id"], format="full",
            ).execute()
            payload = msg.get("payload", {}) or {}

            received = None
            internal = msg.get("internalDate")
            if internal:
                try:
                    received = datetime.fromtimestamp(
                        int(internal) / 1000, tz=timezone.utc
                    )
                except Exception:
                    received = None

            out.append(InboxMessage(
                uid=stub["id"],
                message_id=self._header(payload, "Message-ID") or f"gmail-{stub['id']}",
                sender=self._header(payload, "From"),
                subject=self._header(payload, "Subject"),
                body=self._extract_body(payload),
                received=received,
            ))
        log.info("Gmail API: %d unread message(s) in the last %d days.",
                 len(out), lookback_days)
        return out

    def mark_seen(self, message: InboxMessage) -> None:
        try:
            self._service.users().messages().modify(
                userId="me", id=message.uid,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()
        except Exception as exc:
            log.debug("Could not mark %s read: %s", message.uid, exc)


class ImapBackend(MailBackend):
    """Legacy IMAP with an App Password. Kept for accounts that still have one."""

    name = "imap"

    def __init__(self, host: str, port: int, mailbox: str,
                 user: str, password: str) -> None:
        self.host, self.port, self.mailbox = host, port, mailbox
        self.user, self.password = user, password

    def account(self) -> str:
        return self.user

    def _connect(self) -> imaplib.IMAP4_SSL:
        if not self.user or not self.password:
            raise RuntimeError(
                "JOB_EMAIL and JOB_EMAIL_APP_PASSWORD are not set, and no Gmail "
                "OAuth token exists. Run `python auth_gmail.py`."
            )
        conn = imaplib.IMAP4_SSL(
            self.host, self.port, ssl_context=ssl.create_default_context()
        )
        last = ""
        for candidate in _password_variants(self.password):
            try:
                conn.login(self.user.strip(), candidate)
                conn.select(self.mailbox)
                return conn
            except imaplib.IMAP4.error as exc:
                last = str(exc)

        try:
            conn.logout()
        except Exception:
            pass
        raise RuntimeError(
            f"{self.host} rejected the credentials for {self.user} ({last}).\n"
            "Google no longer issues App Passwords on newer accounts -- if the "
            "setting is missing from your account, that is expected and IMAP "
            "cannot work. Use the Gmail API instead:\n"
            "  python auth_gmail.py\n"
            "Otherwise check: 2-Step Verification is on, the App Password is "
            "current, and IMAP is enabled in Gmail settings."
        )

    def fetch_unread(self, lookback_days: int, limit: int) -> list[InboxMessage]:
        conn = self._connect()
        out: list[InboxMessage] = []
        try:
            since = (datetime.now(timezone.utc)
                     - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
            status, data = conn.search(None, f"(UNSEEN SINCE {since})")
            if status != "OK":
                return []
            uids = (data[0] or b"").split()[-limit:]
            log.info("IMAP: %d unread message(s) in the last %d days.",
                     len(uids), lookback_days)

            for uid in uids:
                # BODY.PEEK does NOT set \Seen -- scanning never marks personal
                # mail as read.
                status, payload = conn.fetch(uid, "(BODY.PEEK[])")
                if status != "OK" or not payload or not payload[0]:
                    continue
                raw = payload[0][1]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                msg = email.message_from_bytes(raw)
                received = None
                if msg.get("Date"):
                    try:
                        received = email.utils.parsedate_to_datetime(msg["Date"])
                    except Exception:
                        received = None
                out.append(InboxMessage(
                    uid=uid.decode(),
                    message_id=_decode(msg.get("Message-ID")) or f"uid-{uid.decode()}",
                    sender=_decode(msg.get("From")),
                    subject=_decode(msg.get("Subject")),
                    body=_body_text(msg),
                    received=received,
                ))
        finally:
            try:
                conn.close()
                conn.logout()
            except Exception:
                pass
        return out

    def mark_seen(self, message: InboxMessage) -> None:
        try:
            conn = self._connect()
            conn.store(message.uid.encode(), "+FLAGS", "\\Seen")
            conn.close()
            conn.logout()
        except Exception as exc:
            log.debug("Could not flag uid %s as seen: %s", message.uid, exc)


def select_backend() -> MailBackend:
    """Gmail API if authorised, else IMAP, else a message explaining both."""
    from auto_apply import gmail_oauth

    if gmail_oauth.is_configured():
        try:
            backend = GmailApiBackend()
            log.info("Mail backend: Gmail API (OAuth2).")
            return backend
        except Exception as exc:
            log.warning("Gmail API unusable (%s); falling back to IMAP.", exc)

    cfg = settings.raw.get("email_monitor", {}) or {}
    if settings.job_email and settings.job_email_password:
        log.info("Mail backend: IMAP (App Password).")
        return ImapBackend(
            host=cfg.get("imap_host", "imap.gmail.com"),
            port=int(cfg.get("imap_port", 993)),
            mailbox=cfg.get("mailbox", "INBOX"),
            user=settings.job_email,
            password=settings.job_email_password,
        )

    raise RuntimeError(
        "No mailbox access configured.\n"
        "  Preferred: python auth_gmail.py    (Gmail API over OAuth2)\n"
        "  Legacy:    set JOB_EMAIL_APP_PASSWORD in .env (older accounts only --\n"
        "             Google no longer issues App Passwords on new ones)."
    )


class EmailMonitor:
    """Reads the mailbox, classifies, records and alerts."""

    def __init__(self, store: SecureStore, notifier: Any = None,
                 backend: MailBackend | None = None):
        self.store = store
        self.notifier = notifier
        cfg = settings.raw.get("email_monitor", {}) or {}
        self.lookback_days = int(cfg.get("lookback_days", 14))
        self.max_messages = int(cfg.get("max_messages", 40))
        self.mark_seen_flag = bool(cfg.get("mark_seen_when_classified", True))
        self._backend = backend

    @property
    def backend(self) -> MailBackend:
        if self._backend is None:
            self._backend = select_backend()
        return self._backend

    def fetch_unread(self) -> list[InboxMessage]:
        return self.backend.fetch_unread(self.lookback_days, self.max_messages)

    def _mark_seen(self, message: InboxMessage) -> None:
        if self.mark_seen_flag:
            self.backend.mark_seen(message)

    # -- classification -----------------------------------------------------
    def classify(self, message: InboxMessage) -> dict[str, Any]:
        from evaluator import GeminiEvaluator

        prompt = (
            f"From: {message.sender}\nSubject: {message.subject}\n\n"
            f"{message.body[:5000]}"
        )
        evaluator = GeminiEvaluator()
        payload, _model = evaluator._generate({        # noqa: SLF001
            "systemInstruction": {"parts": [{"text": CLASSIFY_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": CLASSIFY_SCHEMA,
                "temperature": 0.0,
                "maxOutputTokens": 1024,
            },
        })
        return evaluator._extract_json(payload)        # noqa: SLF001

    # -- alerting -----------------------------------------------------------
    @staticmethod
    def format_alert(result: dict[str, Any], app: dict[str, Any] | None) -> str:
        kind = str(result.get("classification", "other")).lower()
        icon, title = {
            "interview": ("\U0001F389", "INTERVIEW INVITATION RECEIVED!"),
            "assessment": ("\U0001F4CB", "ASSESSMENT / TEST RECEIVED"),
            "rejection": ("\U0001F4EA", "Application closed"),
            "acknowledgment": ("\U0001F4E8", "Application acknowledged"),
            "recruiter_outreach": ("\U0001F4EC", "RECRUITER REACHED OUT"),
        }.get(kind, ("\U0001F4E7", "Job-related email"))

        submitted = "not linked to a tracked application"
        if app:
            submitted = (f"#{app['id']} \u2014 {app.get('role')} via "
                         f"{app.get('platform')}, {str(app.get('submitted_at'))[:16]}")

        lines = [
            f"{icon} *{title}*",
            f"\U0001F3E2 Company: {result.get('company') or '(unknown)'}",
            f"\U0001F4BC Role Applied: "
            f"{result.get('role') or (app or {}).get('role') or '(unknown)'}",
        ]
        if result.get("meeting_datetime"):
            lines.append(f"\U0001F4C5 Date & Time: {result['meeting_datetime']}")
        if result.get("meeting_link"):
            lines.append(f"\U0001F517 Meeting Link: {result['meeting_link']}")
        if result.get("action_required"):
            lines.append(f"\u2757 Action: {result['action_required']}")
        lines.append(f"\U0001F4C4 Submitted Application Data: {submitted}")
        if result.get("summary"):
            lines.append(f"\n{result['summary']}")
        return "\n".join(lines)

    # -- the pass -----------------------------------------------------------
    def run_once(self) -> dict[str, int]:
        """One sweep. Returns counters. Never raises."""
        counts = {"scanned": 0, "skipped": 0, "classified": 0, "alerted": 0}
        try:
            messages = self.fetch_unread()
        except Exception as exc:
            log.error("Could not read the mailbox: %s", exc)
            return counts

        for message in messages:
            counts["scanned"] += 1
            if self.store.seen_message(message.message_id):
                continue
            if not message.looks_job_related():
                # Never sent to Gemini, never flagged, never stored.
                counts["skipped"] += 1
                continue

            try:
                result = self.classify(message)
            except Exception as exc:
                log.warning("Classification failed for %r: %s",
                            message.subject[:60], exc)
                continue

            if not result.get("is_job_related"):
                counts["skipped"] += 1
                continue

            counts["classified"] += 1
            kind = str(result.get("classification", "other")).lower()
            link = result.get("meeting_link") or ""
            if not link:
                links = message.candidate_links()
                link = links[0] if links else ""

            app = self.store.find_application_by_company(result.get("company", ""))
            should_alert = kind in ("interview", "assessment", "recruiter_outreach")

            self.store.record_email_event(
                message_id=message.message_id, sender=message.sender,
                subject=message.subject, classification=kind,
                parsed_date=result.get("meeting_datetime", ""),
                meeting_link=link, summary=result.get("summary", ""),
                matched_app_id=(app or {}).get("id"), alerted=should_alert,
            )

            if should_alert and self.notifier:
                result["meeting_link"] = link
                self.notifier.send_via_telegram(self.format_alert(result, app))
                counts["alerted"] += 1
                log.info("ALERTED: %s from %s", kind, result.get("company"))
            else:
                log.info("Recorded %s from %s (no alert).", kind,
                         result.get("company"))

            self._mark_seen(message)

        log.info(
            "Inbox pass via %s: %d scanned, %d not job mail, %d classified, "
            "%d alerted.",
            self.backend.name, counts["scanned"], counts["skipped"],
            counts["classified"], counts["alerted"],
        )
        return counts
