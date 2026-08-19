"""
Inbound commands from WhatsApp, over a webhook.

WHY THIS IS A WEBHOOK AND NOT A POLLER
--------------------------------------
CallMeBot -- the transport that carries every outbound WhatsApp card in this
system -- is SEND-ONLY. It exposes one HTTP GET that queues a message and
nothing at all for receiving. There is no endpoint to poll, no inbox to read.
So the reply half of WhatsApp cannot be built on it, and pretending otherwise
would produce a listener that silently never fires.

Receiving a WhatsApp message requires one of these, and every one of them is a
credential the user has to obtain -- none can be derived from what is already
configured:

  META CLOUD API   the official route. A Meta app, a registered business
                   number, and a public HTTPS callback. Verified by
                   `X-Hub-Signature-256`.
  TWILIO           a Twilio number with a WhatsApp sender. Posts form-encoded.
  RELAY            anything you control that can POST JSON here -- a Baileys
                   bridge, an n8n flow, a phone-side shortcut.

So this module is the part that CAN be built without a credential: one HTTP
endpoint, three payload adapters, and the same execution pipeline Telegram
uses. Point any of the three at it and replies start working; until then it
runs, answers health checks, and rejects everything unauthenticated.

SECURITY, BECAUSE THIS ENDPOINT CAN FILE A JOB APPLICATION
----------------------------------------------------------
It is a public HTTP surface with the power to submit an application in the
user's name, so a request is executed only if it clears all of:

  1. AUTHENTICATED. An HMAC signature (Meta) or a shared secret header. A
     request with neither is refused before its body is even parsed.
  2. FROM THE RIGHT NUMBER. The sender must be the configured WhatsApp
     number. Anyone who learns the URL still cannot drive it.
  3. NOT A REPLAY. Message ids are remembered; the same id never executes
     twice, however many times the relay re-delivers it.
  4. RECENT, NOT OURS, AND A REAL COMMAND -- the same three gates the Telegram
     listener applies, shared rather than re-implemented.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from auto_apply.control import Reply, ReviewController, hitl_cfg
from auto_apply.review import BOT_MARK
from config import settings

log = logging.getLogger(__name__)


def whatsapp_cfg() -> dict[str, Any]:
    return (hitl_cfg().get("whatsapp_inbound", {}) or {})


def inbound_enabled() -> bool:
    return bool(whatsapp_cfg().get("enabled", False))


@dataclass(slots=True)
class InboundCommand:
    """One command, normalised away from whichever relay delivered it."""

    text: str
    sender: str = ""
    message_id: str = ""
    when: datetime | None = None
    source: str = "whatsapp"
    relay: str = "generic"


# ---------------------------------------------------------------------------
# Payload adapters
# ---------------------------------------------------------------------------
def _digits(value: Any) -> str:
    """Compare phone numbers by digits alone.

    "+20 100 000 7582", "whatsapp:+201000007582" and "201000007582" are the
    same person, and a relay will use whichever it prefers.
    """
    return re.sub(r"\D+", "", str(value or ""))


def parse_meta(payload: dict[str, Any]) -> list[InboundCommand]:
    """Meta WhatsApp Cloud API webhook body."""
    out: list[InboundCommand] = []
    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            value = (change or {}).get("value") or {}
            for message in value.get("messages") or []:
                if message.get("type") not in (None, "text"):
                    continue
                body = ((message.get("text") or {}).get("body") or "").strip()
                if not body:
                    continue
                stamp = None
                try:
                    stamp = datetime.fromtimestamp(
                        int(message.get("timestamp")), tz=timezone.utc
                    )
                except (TypeError, ValueError):
                    pass
                out.append(InboundCommand(
                    text=body, sender=str(message.get("from") or ""),
                    message_id=str(message.get("id") or ""), when=stamp,
                    relay="meta",
                ))
    return out


def parse_twilio(form: dict[str, Any]) -> list[InboundCommand]:
    """Twilio's form-encoded WhatsApp webhook."""
    body = str(form.get("Body") or "").strip()
    if not body:
        return []
    return [InboundCommand(
        text=body, sender=str(form.get("From") or ""),
        message_id=str(form.get("MessageSid") or ""), relay="twilio",
    )]


def parse_generic(payload: dict[str, Any]) -> list[InboundCommand]:
    """Anything you control: a Baileys bridge, n8n, a phone shortcut.

    Accepts the handful of key spellings such relays actually use, so a bridge
    does not have to be rewritten to talk to this.
    """
    text = str(
        payload.get("text") or payload.get("body")
        or payload.get("message") or ""
    ).strip()
    if not text:
        return []
    sender = str(
        payload.get("from") or payload.get("sender")
        or payload.get("phone") or ""
    )
    return [InboundCommand(
        text=text, sender=sender,
        message_id=str(payload.get("id") or payload.get("message_id") or ""),
        relay="generic",
    )]


def parse_payload(payload: Any) -> list[InboundCommand]:
    """Detect the relay from the payload's own shape and normalise it."""
    if not isinstance(payload, dict):
        return []
    if payload.get("object") == "whatsapp_business_account" or "entry" in payload:
        return parse_meta(payload)
    if "MessageSid" in payload or "SmsMessageSid" in payload:
        return parse_twilio(payload)
    return parse_generic(payload)


# ---------------------------------------------------------------------------
# The listener
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class InboundStats:
    received: int = 0
    unauthorised: int = 0
    wrong_sender: int = 0
    replayed: int = 0
    skipped_own: int = 0
    unrecognised: int = 0
    executed: int = 0


class WhatsAppCommandListener:
    """Turns an authenticated webhook POST into the same action Telegram takes.

    Deliberately holds no transport of its own for REPLIES: the controller
    already dispatches every outcome to both channels, so a command that
    arrives on WhatsApp is answered on WhatsApp and Telegram both, exactly as
    one that arrived on Telegram is.
    """

    def __init__(self, controller: ReviewController, *,
                 allowed_number: str = "", app_secret: str = "",
                 shared_secret: str = "", max_age_minutes: int | None = None,
                 seen_limit: int = 500) -> None:
        cfg = whatsapp_cfg()
        self.controller = controller
        self.allowed = _digits(
            allowed_number or cfg.get("allowed_number") or settings.whatsapp_phone
        )
        self.app_secret = str(app_secret or cfg.get("app_secret") or "")
        self.shared_secret = str(shared_secret or cfg.get("shared_secret") or "")
        self.verify_token = str(cfg.get("verify_token") or "")
        self.max_age_minutes = int(
            max_age_minutes if max_age_minutes is not None
            else hitl_cfg().get("max_command_age_minutes", 180)
        )
        self._seen: list[str] = []
        self._seen_set: set[str] = set()
        self._seen_limit = seen_limit
        self._lock = threading.Lock()
        self.stats = InboundStats()

    # -- gates --------------------------------------------------------------
    def authorised(self, *, body: bytes = b"", signature: str = "",
                   token: str = "") -> bool:
        """Is this request allowed to run anything at all?

        Fails CLOSED. With no secret configured the endpoint refuses
        everything rather than accepting everything -- an unauthenticated URL
        that can submit job applications is not a degraded mode, it is a
        vulnerability.
        """
        if self.app_secret and signature:
            expected = "sha256=" + hmac.new(
                self.app_secret.encode(), body, hashlib.sha256
            ).hexdigest()
            if hmac.compare_digest(expected, signature):
                return True
            log.warning("Rejected an inbound webhook: bad HMAC signature.")
            return False
        if self.shared_secret and token:
            if hmac.compare_digest(self.shared_secret, token):
                return True
            log.warning("Rejected an inbound webhook: wrong shared secret.")
            return False
        log.warning("Rejected an unauthenticated inbound webhook.")
        return False

    def from_allowed_number(self, sender: str) -> bool:
        """Only the configured phone may drive this."""
        if not self.allowed:
            return False            # unconfigured means nobody, not everybody
        digits = _digits(sender)
        return bool(digits) and (
            digits == self.allowed
            or digits.endswith(self.allowed) or self.allowed.endswith(digits)
        )

    def is_fresh(self, when: datetime | None) -> bool:
        if when is None or self.max_age_minutes <= 0:
            return True
        from datetime import timedelta

        stamp = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - stamp <= timedelta(
            minutes=self.max_age_minutes
        )

    def already_seen(self, message_id: str) -> bool:
        """Relays retry. A retried approval must not submit twice."""
        if not message_id:
            return False
        with self._lock:
            if message_id in self._seen_set:
                return True
            self._seen.append(message_id)
            self._seen_set.add(message_id)
            while len(self._seen) > self._seen_limit:
                self._seen_set.discard(self._seen.pop(0))
        return False

    # -- execution ----------------------------------------------------------
    def handle(self, command: InboundCommand) -> Reply | None:
        """Run one normalised command. None means "not for us"."""
        self.stats.received += 1
        if BOT_MARK in command.text:
            self.stats.skipped_own += 1
            return None
        if not self.from_allowed_number(command.sender):
            self.stats.wrong_sender += 1
            log.warning("Ignoring a WhatsApp command from %r.",
                        command.sender[:20])
            return None
        if self.already_seen(command.message_id):
            self.stats.replayed += 1
            log.info("Ignoring a re-delivered WhatsApp message.")
            return None
        if not self.is_fresh(command.when):
            log.info("Ignoring a stale WhatsApp command.")
            return None

        reply = self.controller.handle(command.text)
        if not reply.handled:
            self.stats.unrecognised += 1
            return None
        self.stats.executed += 1
        log.info("WhatsApp command '%s' executed via the %s relay.",
                 reply.action, command.relay)
        return reply

    def handle_payload(self, payload: Any, *, raw: bytes = b"",
                       signature: str = "", token: str = "") -> list[Reply]:
        """Authenticate, normalise, and run every command in one webhook body."""
        if not self.authorised(body=raw, signature=signature, token=token):
            self.stats.unauthorised += 1
            raise PermissionError("unauthenticated inbound webhook")
        replies = []
        for command in parse_payload(payload):
            reply = self.handle(command)
            if reply is not None:
                replies.append(reply)
        return replies


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------
def build_handler(listener: WhatsAppCommandListener) -> Any:
    """A stdlib BaseHTTPRequestHandler bound to this listener.

    stdlib on purpose: an inbound webhook should not drag a web framework into
    a project whose entire runtime is otherwise `requests` and Playwright.
    """
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        server_version = "AIJobHunter/1.0"

        def _reply(self, code: int, body: str = "") -> None:
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:            # noqa: N802 - stdlib naming
            # Meta verifies a new webhook by GET before it will deliver
            # anything, echoing the challenge back proves we own the endpoint.
            from urllib.parse import parse_qs, urlsplit

            query = parse_qs(urlsplit(self.path).query)
            mode = (query.get("hub.mode") or [""])[0]
            token = (query.get("hub.verify_token") or [""])[0]
            challenge = (query.get("hub.challenge") or [""])[0]
            if mode == "subscribe" and listener.verify_token and \
                    hmac.compare_digest(token, listener.verify_token):
                log.info("WhatsApp webhook verified by Meta.")
                return self._reply(200, challenge)
            if urlsplit(self.path).path.rstrip("/") in ("/health", ""):
                return self._reply(200, "ok")
            self._reply(403, "forbidden")

        def do_POST(self) -> None:           # noqa: N802 - stdlib naming
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            signature = self.headers.get("X-Hub-Signature-256", "")
            token = (self.headers.get("X-Relay-Token")
                     or self.headers.get("X-Webhook-Token") or "")

            content_type = (self.headers.get("Content-Type") or "").lower()
            try:
                if "application/x-www-form-urlencoded" in content_type:
                    from urllib.parse import parse_qs

                    parsed = {
                        k: v[0] for k, v in
                        parse_qs(raw.decode("utf-8", "replace")).items()
                    }
                else:
                    parsed = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception:
                return self._reply(400, "unparseable body")

            try:
                replies = listener.handle_payload(
                    parsed, raw=raw, signature=signature, token=token
                )
            except PermissionError:
                return self._reply(401, "unauthorised")
            except Exception as exc:         # never leak a traceback publicly
                log.exception("Inbound webhook failed: %s", exc)
                return self._reply(200, "error")
            # 200 regardless of whether anything matched: a relay that gets a
            # non-2xx retries, and retrying a message we deliberately ignored
            # would loop forever.
            self._reply(200, f"ok {len(replies)}")

        def log_message(self, *_args: Any) -> None:
            pass                              # keep request noise out of the log

    return Handler


def serve_whatsapp_inbound(
    listener: WhatsAppCommandListener, host: str = "", port: int = 0,
) -> tuple[Any, threading.Thread]:
    """Start the webhook server on a daemon thread. Returns (server, thread)."""
    from http.server import ThreadingHTTPServer

    cfg = whatsapp_cfg()
    host = host or str(cfg.get("host") or "0.0.0.0")
    port = int(port or cfg.get("port") or 8080)

    server = ThreadingHTTPServer((host, port), build_handler(listener))
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                             name="whatsapp-inbound")
    thread.start()
    log.info(
        "WhatsApp inbound webhook listening on http://%s:%d/  "
        "(point your relay here; it needs a public HTTPS tunnel)",
        host or "0.0.0.0", server.server_address[1],
    )
    return server, thread


def readiness() -> tuple[bool, str]:
    """Can inbound WhatsApp actually work right now? Honest answer.

    Called at startup so `--listen` says what is and is not live, rather than
    printing "listening on both channels" over a channel that cannot receive.
    """
    cfg = whatsapp_cfg()
    if not cfg.get("enabled", False):
        return False, (
            "WhatsApp inbound is off (hitl.whatsapp_inbound.enabled: false). "
            "Outbound WhatsApp cards still go out; replies are Telegram-only."
        )
    if not (cfg.get("app_secret") or cfg.get("shared_secret")):
        return False, (
            "WhatsApp inbound is enabled but has no app_secret or "
            "shared_secret, so every request would be refused. Set one in "
            "config.yml -- an unauthenticated endpoint that can submit job "
            "applications is not a degraded mode, it is a vulnerability."
        )
    if not (cfg.get("allowed_number") or settings.whatsapp_phone):
        return False, "WhatsApp inbound has no allowed_number configured."
    return True, "WhatsApp inbound ready."
