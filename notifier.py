"""
WhatsApp dispatch via CallMeBot.

CallMeBot is a free relay: one HTTPS GET queues a WhatsApp message to a number
that has previously authorised the bot. It has real, undocumented constraints
that this module handles explicitly, because each one is a silent-failure mode
in production:

  * The whole message travels in a QUERY STRING, so it must be percent-encoded
    and kept under a safe URL length -- an over-long alert is dropped, not
    truncated. `_fit_to_budget` shortens the least important field instead.
  * The service throttles aggressively; back-to-back sends get swallowed. Sends
    are spaced by `alert_interval_seconds`.
  * A failed send returns HTTP 200 with an error sentence in the HTML BODY, so
    the status code alone is worthless. `_classify` reads the body.
  * WhatsApp renders *bold* with single asterisks -- not markdown's **.
"""

from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import quote

import http_client
from config import settings
from db import Database
from models import Evaluation

log = logging.getLogger(__name__)

API_URL = "https://api.callmebot.com/whatsapp.php"

# CallMeBot silently drops very long URLs. Stay well inside the limit.
MAX_URL_LENGTH = 1800
MAX_TEXT_CHARS = 900

_SUCCESS = re.compile(r"(message queued|message sent|queued\.)", re.I)
_FAILURE_HINTS = (
    ("apikey", "The CALLMEBOT_APIKEY is wrong, or it was issued for a different "
               "phone number."),
    ("not registered", "This number has not activated CallMeBot. Send "
                       "'I allow callmebot to send me messages' to +34 644 51 95 23 "
                       "on WhatsApp, then use the API key it replies with."),
    ("wait", "CallMeBot is rate-limiting; the next run will retry."),
    ("limit", "CallMeBot usage limit reached for now."),
)


@dataclass(slots=True)
class DispatchResult:
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def _strip_control(text: str) -> str:
    """Remove characters that break URL encoding or render as tofu."""
    return "".join(ch for ch in text if ch == "\n" or ch >= " ")


def _clean_field(value: str, limit: int) -> str:
    value = html.unescape(str(value or "")).strip()
    value = re.sub(r"\s+", " ", value)
    return value[:limit]


class WhatsAppNotifier:
    """Formats and delivers alerts. Honours DRY_RUN."""

    channel = "callmebot"

    def __init__(self, db: Database | None = None):
        self.db = db
        self.phone = settings.whatsapp_phone
        self.apikey = settings.callmebot_apikey
        self.interval = float(settings.engine.get("alert_interval_seconds", 8))
        self.dry_run = settings.dry_run
        self._last_send = 0.0

    # -- formatting ---------------------------------------------------------
    @staticmethod
    def _fit_to_budget(reason: str, gaps: str, overflow: int) -> tuple[str, str]:
        """Shed the least important content first: gaps, then the reason."""
        if overflow <= 0:
            return reason, gaps
        if gaps:
            trim = min(len(gaps), overflow)
            gaps = gaps[: len(gaps) - trim].rstrip(" ,;") + ("..." if trim else "")
            overflow -= trim
        if overflow > 0 and reason:
            keep = max(60, len(reason) - overflow)
            reason = reason[:keep].rstrip(" ,;") + "..."
        return reason, gaps

    def format_alert(self, ev: Evaluation) -> str:
        company = _clean_field(ev.company_name, 90) or "Unknown"
        location = _clean_field(ev.location, 90) or "Unknown"
        role = _clean_field(ev.role_title, 120) or "Unknown"
        link = _clean_field(ev.direct_link, 400) or "(no direct link in the posting)"
        source = _clean_field(ev.source_platform, 60) or "unknown"
        reason = _clean_field(ev.why_matched, 420)
        gaps = _clean_field(", ".join(ev.skill_gaps), 220)

        fixed = len(company) + len(location) + len(role) + len(link) + len(source) + 140
        reason, gaps = self._fit_to_budget(reason, gaps, fixed + len(reason) + len(gaps) - MAX_TEXT_CHARS)

        lines = [
            f"\U0001F6A8 *NEW HIGH-MATCH JOB FOUND* (Score: {ev.match_score}%)",
            f"\U0001F3E2 *Company:* {company}",
            f"\U0001F4CD *Location:* {location}",
            f"\U0001F4BC *Role:* {role}",
            f"\U0001F517 *Link:* {link}",
            f"\U0001F4E1 *Source:* {source}",
        ]
        if reason:
            lines.append(f"✅ *Why You Match:* {reason}")
        if gaps:
            lines.append(f"⚠️ *Gaps to address:* {gaps}")
        return _strip_control("\n".join(lines))

    # -- transport ----------------------------------------------------------
    @staticmethod
    def _classify(body: str) -> tuple[bool, str]:
        text = re.sub(r"<[^>]+>", " ", body or "")
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        if _SUCCESS.search(text):
            return True, "queued"
        low = text.lower()
        for needle, explanation in _FAILURE_HINTS:
            if needle in low:
                return False, explanation
        return False, text[:200] or "empty response from CallMeBot"

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_send
        if self._last_send and elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last_send = time.monotonic()

    def send_raw(self, message: str) -> tuple[bool, str]:
        """Deliver one message. Returns (ok, detail). Never raises."""
        if self.dry_run:
            log.info("[DRY_RUN] would send:\n%s\n", message)
            return True, "dry_run"

        text = message[:MAX_TEXT_CHARS]
        url = (
            f"{API_URL}?phone={quote(self.phone)}"
            f"&apikey={quote(self.apikey)}&text={quote(text)}"
        )
        if len(url) > MAX_URL_LENGTH:
            keep = MAX_TEXT_CHARS - (len(url) - MAX_URL_LENGTH) // 3 - 16
            text = text[: max(120, keep)] + "..."
            url = (
                f"{API_URL}?phone={quote(self.phone)}"
                f"&apikey={quote(self.apikey)}&text={quote(text)}"
            )

        self._throttle()
        try:
            resp = http_client.get(url, timeout=60, respect_circuit=False)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:150]}"
        return self._classify(resp.text)

    # -- high level ---------------------------------------------------------
    def dispatch(self, evaluations: Iterable[Evaluation]) -> DispatchResult:
        """Send one alert per evaluation, recording every outcome."""
        result = DispatchResult()

        for ev in evaluations:
            if self.db and self.db.already_alerted(ev.fingerprint):
                log.info("Already alerted about %r -- skipping.", ev.role_title[:60])
                result.skipped += 1
                continue

            ok, detail = self.send_raw(self.format_alert(ev))
            if ok:
                result.sent += 1
                log.info(
                    "Alert sent: %s @ %s (score %d)",
                    ev.role_title[:60], ev.company_name[:40], ev.match_score,
                )
            else:
                result.failed += 1
                result.errors.append(detail)
                log.error("Alert FAILED for %r: %s", ev.role_title[:60], detail)

            if self.db:
                # A dry run must NOT be banked as 'sent'. `already_alerted()`
                # only counts 'sent', so recording it here would permanently
                # suppress a job the user never actually received.
                if self.dry_run:
                    status = "dry_run"
                elif ok:
                    status = "sent"
                else:
                    status = "failed"
                self.db.record_alert(ev.fingerprint, self.channel, status, detail)
        return result

    # -- operational messages ----------------------------------------------
    def send_failure_alert(self, summary: str) -> bool:
        cooldown = int(settings.notifications.get("failure_cooldown_minutes", 180))
        if self.db and self.db.cooldown_active("failure", cooldown):
            log.info("Failure alert suppressed (cooldown active).")
            return False
        message = (
            "⚠️ *AI JOB HUNTER - PIPELINE PROBLEM*\n"
            f"{_clean_field(summary, 600)}\n\n"
            "Job hunting is paused until this clears. Check the GitHub Actions log."
        )
        ok, detail = self.send_raw(message)
        if ok and self.db:
            self.db.mark_cooldown("failure")
        if not ok:
            log.error("Could not deliver the failure alert either: %s", detail)
        return ok

    def send_heartbeat(self, summary: str) -> bool:
        ok, _ = self.send_raw(
            "\U0001F493 *AI JOB HUNTER - HEARTBEAT*\n" + _clean_field(summary, 700)
        )
        return ok

    def selftest(self) -> tuple[bool, str]:
        return self.send_raw(
            "✅ *AI JOB HUNTER - CONNECTION TEST*\n"
            "CallMeBot is wired up correctly.\n"
            f"Alerts will arrive on this number for every job scoring "
            f"{settings.match_threshold}% or higher."
        )
