"""
Dual-channel alert dispatch.

Every match goes out on BOTH channels, carrying DIFFERENT content, tied
together by a short reference the reader can quote (#101, #102 ...):

  WhatsApp (CallMeBot)  -- the lightweight bilingual card. English metadata
    (company, role, location, salary, score) plus two or three short Arabic
    lines, and deliberately NO application URL. It ends with a pointer:
    "Search Telegram Saved Messages for #101".

  Telegram (Saved Messages) -- the full master card. Same reference, the full
    English reasoning, the Arabic read-out, and the clickable link.

WHY THE WHATSAPP CARD HAS NO LINK. The message travels percent-encoded in a
query string with a hard URL ceiling, and CallMeBot DROPS what overflows rather
than truncating it. Job URLs run past 400 characters; Arabic costs ~5.6 URL
characters per character. Carrying both would routinely blow the budget and
lose the alert silently. Dropping the link buys back roughly 400 characters and
makes room for the Arabic that actually helps the reader triage.

Other constraints this module handles, each a silent-failure mode in production:

  * Budgeting counts ENCODED length, never characters. A 509-character Arabic
    message is 1,985 URL characters.
  * CallMeBot answers datacentre IPs with HTTP 403, so a cloud run would deliver
    nothing at all. Telegram has no such restriction, which is why it is a real
    channel here and not merely a fallback.
  * The service throttles hard; sends are spaced by `alert_interval_seconds`.
  * A failed send returns HTTP 200 with the error in the HTML BODY, so the
    status code alone is worthless -- `_classify` reads the body.
  * WhatsApp renders *bold* with single asterisks, not markdown's **.
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
    @staticmethod
    def _ref(ev: Evaluation) -> str:
        return f"#{ev.ref_id}" if ev.ref_id else "#--"

    def format_whatsapp_card(
        self, ev: Evaluation, telegram_delivered: bool = True
    ) -> str:
        """The lightweight bilingual card. Carries NO application URL.

        Dropping the link is what keeps this message small and reliable: job
        URLs run to 400+ characters, they percent-encode badly, and CallMeBot
        drops anything that overflows its query-string budget rather than
        truncating it. The reader gets the metadata and a short Arabic read-out,
        then pulls the full card (with the clickable link) out of Telegram by
        its reference number.

        If Telegram did NOT accept the card, the pointer would be dangling, so
        the raw URL is included here instead -- worse formatting, but a working
        alert beats a tidy dead end.
        """
        ref = self._ref(ev)
        company = _clean_field(ev.company_name, 70) or "Unknown"
        role = _clean_field(ev.role_title, 90) or "Unknown"
        location = _clean_field(ev.location, 70) or "Unknown"
        salary = _clean_field(ev.salary, 60)
        source = _clean_field(ev.source_platform, 40) or "unknown"

        summary_ar = _clean_field(ev.arabic_summary, 140)
        why_ar = _clean_field(ev.why_matched_ar, 140)
        gaps_ar = _clean_field(ev.gaps_ar, 110)

        def build(s_ar: str, w_ar: str, g_ar: str) -> str:
            head = [
                f"\U0001F6A8 *NEW HIGH-MATCH JOB* {ref} ({ev.match_score}%)",
                f"\U0001F3E2 *Company:* {company}",
                f"\U0001F4BC *Role:* {role}",
                f"\U0001F4CD *Location:* {location}",
            ]
            if salary:
                head.append(f"\U0001F4B0 *Salary:* {salary}")
            head.append(f"\U0001F4E1 *Source:* {source}")

            body: list[str] = []
            if s_ar:
                body.append(f"\U0001F4DD {s_ar}")
            if w_ar:
                body.append(f"✅ {w_ar}")
            if g_ar:
                body.append(f"⚠️ {g_ar}")

            if telegram_delivered:
                tail = f"\U0001F517 *Link:* Search Telegram Saved Messages for {ref}"
            else:
                link = _clean_field(ev.direct_link, 300)
                tail = f"\U0001F517 *Link:* {link}" if link else \
                       "\U0001F517 *Link:* (none in the posting)"

            parts = ["\n".join(head)]
            if body:
                parts.append("\n".join(body))
            parts.append(tail)
            return _strip_control("\n\n".join(parts))

        # Arabic costs ~5.6 URL characters each, so roughly 195 Arabic
        # characters fit alongside the English shell. Shed the least important
        # line first (gaps), then the reasoning, then the summary.
        while _over_budget(build(summary_ar, why_ar, gaps_ar)) and (
            summary_ar or why_ar or gaps_ar
        ):
            if gaps_ar:
                gaps_ar = self._shrink(gaps_ar)
            elif why_ar:
                why_ar = self._shrink(why_ar)
            else:
                summary_ar = self._shrink(summary_ar)
        return build(summary_ar, why_ar, gaps_ar)

    def format_telegram_card(self, ev: Evaluation) -> str:
        """The full master card: every detail plus the clickable link.

        Telegram allows 4096 characters and no URL encoding, so nothing here
        needs shortening. This is the record the WhatsApp card points at.
        """
        ref = self._ref(ev)
        lines = [
            f"\U0001F6A8 NEW HIGH-MATCH JOB {ref}  ({ev.match_score}%)",
            "",
            f"\U0001F3E2 Company:  {_clean_field(ev.company_name, 120) or 'Unknown'}",
            f"\U0001F4BC Role:     {_clean_field(ev.role_title, 160) or 'Unknown'}",
            f"\U0001F4CD Location: {_clean_field(ev.location, 120) or 'Unknown'}",
        ]
        if ev.salary:
            lines.append(f"\U0001F4B0 Salary:   {_clean_field(ev.salary, 90)}")
        lines.append(f"\U0001F4E1 Source:   {_clean_field(ev.source_platform, 60)}")
        lines.append("")
        lines.append(
            f"\U0001F517 Link: {ev.direct_link or '(no direct link in the posting)'}"
        )

        if ev.why_matched:
            lines += ["", f"✅ Why you match: {_clean_field(ev.why_matched, 700)}"]
        if ev.skill_gaps:
            lines.append(f"⚠️ Gaps: {_clean_field(', '.join(ev.skill_gaps), 400)}")

        arabic = [t for t in (
            _clean_field(ev.arabic_summary, 300),
            _clean_field(ev.why_matched_ar, 300),
            _clean_field(ev.gaps_ar, 200),
        ) if t]
        if arabic:
            lines += ["", "\U0001F4DD " + "\n✅ ".join(arabic[:2])]
            if len(arabic) > 2:
                lines.append("⚠️ " + arabic[2])

        lines += ["", f"\U0001F50E Reference: {ref}"]
        return _strip_control("\n".join(lines))[:4000]

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

    def _send_callmebot(self, message: str) -> tuple[bool, str]:
        """WhatsApp only, with no fallback. Never raises.

        Job alerts use this directly because they go down BOTH channels with
        DIFFERENT content -- falling back here would resend the WhatsApp card
        (the one with no link) to Telegram, which already has the better one.
        Single-message traffic like the digest uses `send_raw`, which does fall
        back.
        """
        if self.dry_run:
            log.info("[DRY_RUN] would WhatsApp:\n%s\n", message)
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
            return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
        return self._classify(resp.text)

    def send_raw(self, message: str) -> tuple[bool, str]:
        """Deliver one message down whichever channel works. Never raises.

        CallMeBot first (WhatsApp is the requested channel), then Telegram --
        see `send_via_telegram` for why that fallback exists. Used for
        single-message traffic: the source digest, failure alerts, heartbeats
        and the self-test. Job alerts use `dispatch`, which sends a tailored
        card to each channel instead.
        """
        ok, detail = self._send_callmebot(message)
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
        """Send each match down BOTH channels, recording every outcome.

        Telegram goes FIRST, deliberately. The WhatsApp card carries no link --
        it says "search Saved Messages for #101" -- so if WhatsApp arrived first
        and Telegram then failed, the reader would be chasing a card that does
        not exist. Sending Telegram first means the pointer is only ever printed
        once its target is already there; if Telegram fails, the WhatsApp card
        falls back to carrying the raw URL instead.

        A match counts as delivered if EITHER channel accepted it.
        """
        result = DispatchResult()

        for ev in evaluations:
            if self.db and self.db.already_alerted(ev.fingerprint):
                log.info("Already alerted about %r -- skipping.", ev.role_title[:60])
                result.skipped += 1
                continue

            # Stable, human-quotable handle shared by both cards.
            if self.db and not ev.ref_id:
                ev.ref_id = self.db.assign_ref_id(ev.fingerprint)

            tg_ok, tg_detail = (False, "telegram unavailable")
            if self.dry_run:
                tg_ok, tg_detail = self.send_via_telegram(
                    self.format_telegram_card(ev)
                )
            elif self._telegram_available():
                tg_ok, tg_detail = self.send_via_telegram(
                    self.format_telegram_card(ev)
                )
                if not tg_ok:
                    log.warning("Telegram card failed for #%s: %s",
                                ev.ref_id, tg_detail[:110])

            wa_ok, wa_detail = self._send_callmebot(
                self.format_whatsapp_card(ev, telegram_delivered=tg_ok)
            )

            delivered = tg_ok or wa_ok
            if delivered:
                result.sent += 1
                log.info(
                    "Alert #%s sent (%s): %s @ %s (score %d)",
                    ev.ref_id or "-",
                    "+".join(c for c, k in (("telegram", tg_ok),
                                            ("whatsapp", wa_ok)) if k),
                    ev.role_title[:52], ev.company_name[:32], ev.match_score,
                )
            else:
                result.failed += 1
                result.errors.append(f"whatsapp: {wa_detail} | telegram: {tg_detail}")
                log.error("Alert #%s FAILED on BOTH channels for %r: %s | %s",
                          ev.ref_id or "-", ev.role_title[:50],
                          wa_detail[:80], tg_detail[:80])

            if self.db:
                # A dry run must NOT be banked as 'sent'. `already_alerted()`
                # only counts 'sent', so recording it here would permanently
                # suppress a job the user never actually received.
                for channel, ok, detail in (
                    ("callmebot", wa_ok, wa_detail),
                    ("telegram", tg_ok, tg_detail),
                ):
                    if self.dry_run:
                        status = "dry_run"
                    elif ok:
                        status = "sent"
                    else:
                        status = "failed"
                    self.db.record_alert(ev.fingerprint, channel, status, detail)
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
