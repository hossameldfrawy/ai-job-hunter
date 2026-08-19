"""
WhatsApp dispatch via CallMeBot.

CallMeBot is a free relay: one HTTPS GET queues a WhatsApp message to a number
that has previously authorised the bot. It has real, undocumented constraints
that this module handles explicitly, because each one is a silent-failure mode
in production:

  * The whole message travels in a QUERY STRING, so it must be percent-encoded
    and kept under a safe URL length -- an over-long alert is DROPPED, not
    truncated. Budgeting therefore counts ENCODED length, never characters:
    Arabic is 2 bytes per character and ~9 URL characters once encoded, so a
    509-character Arabic alert is 1,985 URL characters. Optional fields are
    shed until it fits; the header, link and source lines never are.
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
from typing import Any, Iterable
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


# Bytes reserved for "https://api.callmebot.com/whatsapp.php?phone=..&apikey=.."
# plus the "(part n/m)" suffix. Generous on purpose: overshooting the URL limit
# means CallMeBot drops the message silently.
_URL_OVERHEAD = 220


def _over_budget(text: str) -> bool:
    """True if `text` will not survive the trip to CallMeBot.

    Counting CHARACTERS is not enough. The message travels percent-encoded in a
    query string, and Arabic is 2 bytes per character -- so `اخصائي` costs 36
    URL characters, not 6. A digest full of Arabic job titles measures well
    under the character cap while being nearly twice the URL limit, and
    CallMeBot answers that by dropping the message rather than truncating it.
    """
    return (
        len(text) > MAX_TEXT_CHARS
        or len(quote(text)) + _URL_OVERHEAD > MAX_URL_LENGTH
    )


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
    def format_alert(self, ev: Evaluation) -> str:
        company = _clean_field(ev.company_name, 90) or "Unknown"
        location = _clean_field(ev.location, 90) or "Unknown"
        role = _clean_field(ev.role_title, 120) or "Unknown"
        link = _clean_field(ev.direct_link, 400) or "(no direct link in the posting)"
        source = _clean_field(ev.source_platform, 60) or "unknown"
        reason = _clean_field(ev.why_matched, 420)
        gaps = _clean_field(", ".join(ev.skill_gaps), 220)

        def build(why: str, missing: str) -> str:
            body = [
                f"\U0001F6A8 *NEW HIGH-MATCH JOB FOUND* (Score: {ev.match_score}%)",
                f"\U0001F3E2 *Company:* {company}",
                f"\U0001F4CD *Location:* {location}",
                f"\U0001F4BC *Role:* {role}",
                f"\U0001F517 *Link:* {link}",
                f"\U0001F4E1 *Source:* {source}",
            ]
            if why:
                body.append(f"✅ *Why You Match:* {why}")
            if missing:
                body.append(f"⚠️ *Gaps to address:* {missing}")
            return _strip_control("\n".join(body))

        # Shed optional content until the message fits the ENCODED budget, not
        # the character count. An Arabic alert runs ~4x longer once percent-
        # encoded, so a 509-character message can be 1,985 URL characters -- and
        # CallMeBot silently drops what it cannot fit rather than truncating.
        # The header, link and source lines are never sacrificed.
        while _over_budget(build(reason, gaps)) and (reason or gaps):
            if gaps:
                gaps = self._shrink(gaps)
            else:
                reason = self._shrink(reason)
        return build(reason, gaps)

    @staticmethod
    def _shrink(text: str) -> str:
        """Drop roughly a quarter of a field, or clear it once it is a stub."""
        if len(text) <= 24:
            return ""
        return text[: int(len(text) * 0.75)].rstrip(" ,;،") + "..."

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

    # -- fallback channel ---------------------------------------------------
    @staticmethod
    def _telegram_available() -> bool:
        try:
            from scrapers.telegram_user_client import telethon_available
        except Exception:
            return False
        return bool(telethon_available() and settings.telegram_ready)

    def send_via_telegram(self, message: str) -> tuple[bool, str]:
        """Deliver to your own Telegram Saved Messages.

        This exists because CallMeBot answers a GitHub Actions runner with
        HTTP 403 -- it blocks datacentre IP ranges. Measured: the identical
        request returns 200 from a home connection and 403 from the cloud, so
        a scheduled run found nine genuine matches and delivered none of them.

        Telegram has no such restriction, and the bot already holds an
        authorised MTProto session for ingestion. Saved Messages is the target
        because it is private, always present, and needs no extra setup.
        Message limit is 4096 characters rather than CallMeBot's URL budget, so
        nothing has to be shortened here.
        """
        if not self._telegram_available():
            return False, "telegram fallback unavailable (no session or Telethon)"

        import asyncio

        from scrapers.telegram_user_client import build_client, ensure_authorized

        async def _send() -> None:
            client = build_client()
            await ensure_authorized(client)
            try:
                await client.send_message("me", message[:4096], link_preview=False)
            finally:
                await client.disconnect()

        try:
            asyncio.run(_send())
            return True, "sent via telegram"
        except Exception as exc:
            return False, f"telegram fallback failed: {type(exc).__name__}: {exc}"[:200]

    def send_raw(self, message: str) -> tuple[bool, str]:
        """Deliver one message. Returns (ok, detail). Never raises.

        Tries CallMeBot first (WhatsApp is the requested channel), then falls
        back to Telegram if configured -- see `send_via_telegram` for why that
        matters in the cloud.
        """
        if self.dry_run:
            log.info("[DRY_RUN] would send:\n%s\n", message)
            return True, "dry_run"

        text = message[:MAX_TEXT_CHARS]

        def build(body: str) -> str:
            return (f"{API_URL}?phone={quote(self.phone)}"
                    f"&apikey={quote(self.apikey)}&text={quote(body)}")

        # Last-resort guard. Callers should already fit the budget, but this
        # must hold for ANY message: trimming a fixed number of CHARACTERS
        # cannot bound a percent-encoded URL, because one Arabic character can
        # cost nine URL characters. Shrink until it actually fits.
        url = build(text)
        while len(url) > MAX_URL_LENGTH and len(text) > 40:
            overshoot = len(url) - MAX_URL_LENGTH
            text = text[: max(40, len(text) - max(8, overshoot // 6))].rstrip() + "..."
            url = build(text)
        if len(url) > MAX_URL_LENGTH:
            log.error("Message cannot be made to fit CallMeBot's URL limit; "
                      "sending anyway may fail.")

        self._throttle()
        try:
            resp = http_client.get(url, timeout=60, respect_circuit=False)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

        if resp.status_code != 200:
            return self._fallback(
                message, f"HTTP {resp.status_code}: {resp.text[:120]}"
            )
        ok, detail = self._classify(resp.text)
        if ok:
            return True, detail
        return self._fallback(message, detail)

    def _fallback(self, message: str, primary_error: str) -> tuple[bool, str]:
        """CallMeBot refused; try Telegram before giving up on the alert."""
        if not settings.notifications.get("telegram_fallback", True):
            return False, primary_error
        if not self._telegram_available():
            return False, primary_error

        log.warning("CallMeBot failed (%s); falling back to Telegram.",
                    primary_error[:90])
        ok, detail = self.send_via_telegram(message)
        if ok:
            return True, f"telegram fallback (callmebot: {primary_error[:60]})"
        return False, f"{primary_error} | {detail}"

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

    # -- source health digest ----------------------------------------------
    #
    # Human-readable names for the scraper keys. Anything not listed falls back
    # to a title-cased version of the key, so a new scraper still appears in the
    # digest without anyone remembering to register it here.
    SOURCE_LABELS: dict[str, str] = {
        "linkedin": "LinkedIn (GCC)",
        "tanqeeb": "Tanqeeb (Arab/GCC)",
        "talent": "Talent.com Regional",
        "telegram": "Telegram Channels",
        "telegram_user": "Telegram User Client",
        "job_apis": "Remote Job APIs",
        "search_proxy": "Search Proxy (Bayt/Wuzzuf)",
        "rss": "RSS Feeds",
        "facebook": "Facebook (indexed)",
    }

    @classmethod
    def _label(cls, name: str) -> str:
        return cls.SOURCE_LABELS.get(name, name.replace("_", " ").title())

    @classmethod
    def format_source_digest(cls, report: Any) -> list[str]:
        """Render the per-source audit as one or more WhatsApp messages.

        The whole point is proof that EVERY source ran, so this must never
        silently drop a source to fit. CallMeBot carries the text in a query
        string with a hard URL ceiling, and nine sources at two lines each
        comfortably exceeds one message -- so the digest SPLITS across numbered
        parts instead of truncating. The notifier already paces sends, so the
        parts arrive in order.
        """
        sources = list(getattr(report, "sources", []) or [])
        # Failures first (ok=False sorts before ok=True), then busiest. A broken
        # source is the only part of this report that needs acting on, so it
        # must not be buried below nine healthy ones.
        sources.sort(key=lambda s: (bool(s.get("ok", True)), -int(s.get("count", 0))))

        blocks: list[str] = []
        for src in sources:
            name = cls._label(str(src.get("name", "?")))
            count = int(src.get("count", 0))
            healthy = bool(src.get("ok", True))

            if not healthy:
                detail = _clean_field(str(src.get("error", "")), 70) or "unknown error"
                blocks.append(f"🔻 *{name}:* FAILED\n└ {detail}")
                continue

            line = f"🔹 *{name}:* {count} job{'' if count == 1 else 's'} scraped"
            sample = src.get("sample") or {}
            title = _clean_field(str(sample.get("title", "")), 58)
            company = _clean_field(str(sample.get("company", "")), 34)
            shown = f"{title} @ {company}" if title and company else title
            # A source can be perfectly reachable and still return nothing worth
            # applying to. Distinguishing the two is the difference between
            # "working" and "working for me".
            on_profile = str(sample.get("relevant", "yes")) == "yes"
            if shown and on_profile:
                line += f'\n└ Last seen: "{shown}"'
            elif shown:
                line += f'\n└ Reachable, none on-profile: "{shown}"'
            elif count == 0:
                line += "\n└ nothing new this run"
            blocks.append(line)

        total = sum(int(s.get("count", 0)) for s in sources)
        healthy = sum(1 for s in sources if s.get("ok", True))
        rule = "─" * 18
        header = f"📊 *SOURCE HEALTH & AUDIT REPORT*\n{rule}"
        footer = (
            f"{rule}\nTotal: {total:,} jobs inspected across "
            f"{healthy}/{len(sources)} platforms."
        )
        if getattr(report, "evaluated", 0):
            footer += (
                f"\nAI-scored {report.evaluated}, matched {report.matched}, "
                f"alerted {report.alerts_sent}."
            )

        # Pack blocks into messages that each stay inside the transport budget.
        # The footer is packed as just another block, so it lands at the end of
        # whichever part has room rather than stranding itself in a near-empty
        # message. Continuation parts get a compact header with no rule, or the
        # divider would print twice in a row.
        cont_header = "📊 *SOURCE AUDIT — continued*"
        parts: list[str] = []
        current = header
        current_header = header

        for block in blocks + [footer]:
            candidate = f"{current}\n{block}"
            if _over_budget(candidate) and current != current_header:
                parts.append(current)
                current_header = cont_header
                current = f"{cont_header}\n{block}"
            else:
                current = candidate
        parts.append(current)

        if len(parts) > 1:
            parts = [f"{p}\n_(part {i}/{len(parts)})_" for i, p in enumerate(parts, 1)]
        return [_strip_control(p) for p in parts]

    def send_source_digest(self, report: Any) -> bool:
        """Deliver the audit digest. Returns True if every part was accepted."""
        parts = self.format_source_digest(report)
        all_ok = True
        for index, part in enumerate(parts, 1):
            ok, detail = self.send_raw(part)
            if not ok:
                all_ok = False
                log.error("Source digest part %d/%d failed: %s",
                          index, len(parts), detail)
        if all_ok:
            log.info("Source health digest sent (%d part(s)).", len(parts))
        return all_ok

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
