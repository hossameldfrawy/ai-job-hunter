"""
Telegram ingestion via the public channel web preview (t.me/s/<channel>).

Telegram serves every PUBLIC channel as plain server-rendered HTML at
`https://t.me/s/<channel>` -- the same page any logged-out browser gets. That
makes this the single most valuable property of this scraper: it needs **no
API ID, no api_hash, no phone number, no session file and no login**, so it
runs unchanged inside a stateless GitHub Actions runner.

(For PRIVATE groups, supergroups and join-restricted channels see
`telegram_user_client.py`, which signs in over MTProto as you and can read
every chat you have actually joined.)

Telegram posts are free-form prose, not structured records, so this scraper
deliberately does only light structuring -- extracting the apply link, the
timestamp and a plausible title -- and hands the full message text to Gemini,
which is far better at reading a messy bilingual Arabic/English job post than
any regex could be.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from bs4 import BeautifulSoup

import http_client
from models import JobPost
from scrapers.base import (
    BaseScraper, clean, derive_post_title, first_url, parse_date,
)

log = logging.getLogger(__name__)

CHANNEL_URL = "https://t.me/s/{channel}"

# A post must look like a vacancy at all -- channels mix in news and adverts.
_JOBBY = re.compile(
    r"(hiring|vacanc|job|position|opening|required|wanted|we are looking|"
    r"apply|recruit|opportunit|مطلوب|وظيف|وظائف|فرصة|تعيين|شاغر)",
    re.I,
)


class TelegramWebScraper(BaseScraper):
    """Scrapes one or many public Telegram channels. Zero credentials."""

    name = "telegram"

    def __init__(self, cfg: dict[str, Any], timeout: int = 25):
        super().__init__(cfg, timeout)
        self.channels = [
            str(c).strip().lstrip("@")
            for c in (cfg.get("channels") or [])
            if str(c).strip()
        ]
        self.limit = int(cfg.get("messages_per_channel", 40))

    # -- fetching -----------------------------------------------------------
    def _fetch_page(self, channel: str, before: int | None = None) -> str:
        url = CHANNEL_URL.format(channel=channel)
        if before:
            url = f"{url}?before={before}"
        return http_client.get_text(url, timeout=self.timeout)

    def _channel_messages(self, channel: str) -> list[tuple[Any, str]]:
        """Return [(message_wrap, channel)] newest-first, paging back as needed.

        t.me serves 20 posts per page; `?before=<post_id>` walks backwards.
        """
        collected: list[Any] = []
        before: int | None = None
        seen_ids: set[str] = set()

        while len(collected) < self.limit:
            html = self._fetch_page(channel, before)
            if not html.strip():
                break
            soup = BeautifulSoup(html, "lxml")
            wraps = soup.select(".tgme_widget_message_wrap")
            if not wraps:
                if not collected:
                    log.info(
                        "[telegram] @%s returned no posts -- it is private, empty "
                        "or the username is wrong.", channel,
                    )
                break

            oldest_id: int | None = None
            fresh = 0
            for wrap in wraps:
                msg = wrap.select_one(".tgme_widget_message")
                post = (msg.get("data-post") if msg else "") or ""
                if post in seen_ids:
                    continue
                seen_ids.add(post)
                collected.append(wrap)
                fresh += 1
                try:
                    pid = int(post.rsplit("/", 1)[-1])
                    oldest_id = pid if oldest_id is None else min(oldest_id, pid)
                except (ValueError, IndexError):
                    pass

            if not fresh or oldest_id is None or oldest_id <= 1:
                break
            before = oldest_id

        return [(w, channel) for w in collected[: self.limit]]

    # -- parsing ------------------------------------------------------------
    @staticmethod
    def _derive_title(text: str) -> str:
        """Delegates to the shared helper -- see scrapers/base.derive_post_title."""
        return derive_post_title(text)

    def _parse_message(self, wrap: Any, channel: str) -> JobPost | None:
        text_el = wrap.select_one(".tgme_widget_message_text")
        if not text_el:
            return None
        text = text_el.get_text("\n", strip=True)
        if len(text) < 40 or not _JOBBY.search(text):
            return None

        msg = wrap.select_one(".tgme_widget_message")
        post_id = (msg.get("data-post") if msg else "") or ""
        permalink = f"https://t.me/{post_id}" if post_id else f"https://t.me/{channel}"

        time_el = wrap.select_one("time")
        posted = parse_date(time_el.get("datetime")) if time_el else None

        # Prefer a real application link over the Telegram permalink.
        external = ""
        for anchor in text_el.select("a[href]"):
            href = anchor.get("href", "")
            if href.startswith("http") and "t.me/" not in href:
                external = href
                break
        if not external:
            candidate = first_url(text)
            if candidate and "t.me/" not in candidate:
                external = candidate

        return JobPost(
            source=f"telegram:{channel}",
            title=self._derive_title(text),
            company="",           # Gemini extracts this from the message body
            location="",          # ditto
            url=external or permalink,
            description=clean(text, 4000),
            posted_at=posted,
            raw={"channel": channel, "permalink": permalink},
        )

    # -- driver -------------------------------------------------------------
    def collect(self) -> Iterable[JobPost]:
        if not self.channels:
            log.warning(
                "[telegram] No channels configured. Add them under telegram.channels "
                "in config.yml (validate first: python discover_channels.py @name)."
            )
            return []

        jobs: list[JobPost] = []
        for channel in self.channels:
            try:
                messages = self._channel_messages(channel)
            except Exception as exc:
                log.warning("[telegram] @%s failed: %s", channel, exc)
                continue
            parsed = 0
            for wrap, chan in messages:
                job = self._parse_message(wrap, chan)
                if job:
                    jobs.append(job)
                    parsed += 1
            log.info("[telegram] @%s -> %d vacancy-like posts (of %d fetched)",
                     channel, parsed, len(messages))
        return jobs
