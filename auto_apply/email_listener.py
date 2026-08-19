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


class EmailMonitor:
    """Reads the mailbox, classifies, records and alerts."""

    def __init__(self, store: SecureStore, notifier: Any = None):
        self.store = store
        self.notifier = notifier
        cfg = settings.raw.get("email_monitor", {}) or {}
        self.host = cfg.get("imap_host", "imap.gmail.com")
        self.port = int(cfg.get("imap_port", 993))
        self.mailbox = cfg.get("mailbox", "INBOX")
        self.lookback_days = int(cfg.get("lookback_days", 14))
        self.max_messages = int(cfg.get("max_messages", 40))
        self.mark_seen = bool(cfg.get("mark_seen_when_classified", True))
        self.user = settings.job_email
        self.password = settings.job_email_password

    # -- connection ---------------------------------------------------------
    def _connect(self) -> imaplib.IMAP4_SSL:
        if not self.user or not self.password:
            raise RuntimeError(
                "JOB_EMAIL and JOB_EMAIL_APP_PASSWORD are not set. Create a "
                "Gmail App Password (Google Account > Security > App passwords) "
                "and put both in .env."
            )

        conn = imaplib.IMAP4_SSL(self.host, self.port)
        # Google displays app passwords in four spaced groups; the spaces are
        # presentational. Try the literal value first, then the stripped form,
        # so a copy-paste straight from the Google page works either way.
        candidates = [self.password]
        squashed = self.password.replace(" ", "")
        if squashed != self.password:
            candidates.append(squashed)

        last = ""
        for candidate in candidates:
            try:
                conn.login(self.user, candidate)
                conn.select(self.mailbox)
                return conn
            except imaplib.IMAP4.error as exc:
                last = str(exc)

        try:
            conn.logout()
        except Exception:
            pass
        raise RuntimeError(
            f"Gmail rejected the credentials for {self.user} ({last}).\n"
            "Check, in order:\n"
            "  1. the mailbox exists and you can sign in to it in a browser;\n"
            "  2. 2-Step Verification is ON (app passwords require it);\n"
            "  3. the App Password is current -- they are shown once and are\n"
            "     revoked whenever the account password changes;\n"
            "  4. IMAP is enabled: Gmail > Settings > Forwarding and POP/IMAP.\n"
            "Generate a fresh one at https://myaccount.google.com/apppasswords "
            "and update JOB_EMAIL_APP_PASSWORD in .env."
        )

    def fetch_unread(self) -> list[InboxMessage]:
        """Return unread messages WITHOUT marking any of them read."""
        conn = self._connect()
        out: list[InboxMessage] = []
        try:
            since = (datetime.now(timezone.utc)
                     - timedelta(days=self.lookback_days)).strftime("%d-%b-%Y")
            status, data = conn.search(None, f'(UNSEEN SINCE {since})')
            if status != "OK":
                return []
            uids = (data[0] or b"").split()[-self.max_messages:]
            log.info("Inbox: %d unread message(s) in the last %d days.",
                     len(uids), self.lookback_days)

            for uid in uids:
                # BODY.PEEK is the whole point: it does NOT set the \Seen flag,
                # so scanning the inbox never marks a personal email as read.
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

    def _mark_seen(self, uid: str) -> None:
        if not self.mark_seen:
            return
        try:
            conn = self._connect()
            conn.store(uid.encode(), "+FLAGS", "\\Seen")
            conn.close()
            conn.logout()
        except Exception as exc:
            log.debug("Could not flag uid %s as seen: %s", uid, exc)

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
            "interview": ("🎉", "INTERVIEW INVITATION RECEIVED!"),
            "assessment": ("📋", "ASSESSMENT / TEST RECEIVED"),
            "rejection": ("📪", "Application closed"),
            "acknowledgment": ("📨", "Application acknowledged"),
            "recruiter_outreach": ("📬", "RECRUITER REACHED OUT"),
        }.get(kind, ("📧", "Job-related email"))

        submitted = "not linked to a tracked application"
        if app:
            submitted = (f"#{app['id']} — {app.get('role')} via "
                         f"{app.get('platform')}, {str(app.get('submitted_at'))[:16]}")

        lines = [
            f"{icon} *{title}*",
            f"🏢 Company: {result.get('company') or '(unknown)'}",
            f"💼 Role Applied: {result.get('role') or (app or {}).get('role') or '(unknown)'}",
        ]
        if result.get("meeting_datetime"):
            lines.append(f"📅 Date & Time: {result['meeting_datetime']}")
        if result.get("meeting_link"):
            lines.append(f"🔗 Meeting Link: {result['meeting_link']}")
        if result.get("action_required"):
            lines.append(f"❗ Action: {result['action_required']}")
        lines.append(f"📄 Submitted Application Data: {submitted}")
        if result.get("summary"):
            lines.append(f"\n{result['summary']}")
        return "\n".join(lines)

    # -- the pass -----------------------------------------------------------
    def run_once(self) -> dict[str, int]:
        """One sweep. Returns counters."""
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

            self._mark_seen(message.uid)

        log.info("Inbox pass: %(scanned)d scanned, %(skipped)d not job mail, "
                 "%(classified)d classified, %(alerted)d alerted.", counts)
        return counts
