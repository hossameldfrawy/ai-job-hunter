"""
Authenticated Telegram ingestion (Telethon) -- OPTIONAL.

`telegram_web.py` already covers every PUBLIC channel with zero credentials and
is the default. This module exists only for the cases the web preview cannot
reach:

  * private channels you have joined
  * channels whose owner disabled the public web preview
  * groups (as opposed to broadcast channels)

It activates only when TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_SESSION
are all present, and degrades to a no-op (never an error) otherwise -- so the
cloud deployment runs fine without ever configuring it.

Generate the session string once, locally:

    python setup_wizard.py --telegram-login

Store the printed StringSession as the TELEGRAM_SESSION secret. It is
equivalent to a login token: keep it in secrets, never in the repository.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable

from config import settings
from models import JobPost
from scrapers.base import BaseScraper, clean, first_url

log = logging.getLogger(__name__)


def _telethon_available() -> bool:
    try:
        import telethon  # noqa: F401
        return True
    except ImportError:
        return False


class TelegramApiScraper(BaseScraper):
    name = "telegram_api"

    def __init__(self, cfg: dict[str, Any], timeout: int = 25):
        super().__init__(cfg, timeout)
        self.channels = [
            str(c).strip().lstrip("@")
            for c in (cfg.get("private_channels") or [])
            if str(c).strip()
        ]
        self.limit = int(cfg.get("messages_per_channel", 40))

    def _configured(self) -> bool:
        return bool(
            settings.telegram_api_id
            and settings.telegram_api_hash
            and settings.telegram_session
            and self.channels
        )

    async def _collect_async(self) -> list[JobPost]:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        jobs: list[JobPost] = []
        client = TelegramClient(
            StringSession(settings.telegram_session),
            int(settings.telegram_api_id),
            settings.telegram_api_hash,
        )
        await client.connect()
        try:
            if not await client.is_user_authorized():
                log.error(
                    "[telegram_api] TELEGRAM_SESSION is invalid or revoked. "
                    "Regenerate it: python setup_wizard.py --telegram-login"
                )
                return []

            for channel in self.channels:
                try:
                    entity = await client.get_entity(channel)
                except Exception as exc:
                    log.warning("[telegram_api] cannot resolve @%s: %s", channel, exc)
                    continue

                count = 0
                async for msg in client.iter_messages(entity, limit=self.limit):
                    text = (msg.message or "").strip()
                    if len(text) < 40:
                        continue
                    posted = getattr(msg, "date", None)
                    permalink = f"https://t.me/{channel}/{msg.id}"
                    external = first_url(text)
                    jobs.append(JobPost(
                        source=f"telegram_api:{channel}",
                        title=text.splitlines()[0][:180],
                        url=external if external and "t.me/" not in external else permalink,
                        description=clean(text, 4000),
                        posted_at=posted,
                        raw={"channel": channel, "message_id": msg.id},
                    ))
                    count += 1
                log.info("[telegram_api] @%s -> %d messages", channel, count)
        finally:
            await client.disconnect()
        return jobs

    def collect(self) -> Iterable[JobPost]:
        if not self._configured():
            log.debug(
                "[telegram_api] Not configured (needs TELEGRAM_API_ID / "
                "TELEGRAM_API_HASH / TELEGRAM_SESSION + private_channels). "
                "Public channels are already covered by the web scraper."
            )
            return []
        if not _telethon_available():
            log.warning(
                "[telegram_api] Configured, but Telethon is not installed. "
                "Run: pip install -r requirements-extra.txt"
            )
            return []

        # Scrapers run inside worker threads, which have no event loop of their
        # own -- asyncio.run() creates and tears one down cleanly.
        try:
            return asyncio.run(self._collect_async())
        except RuntimeError as exc:
            log.warning("[telegram_api] event-loop problem: %s", exc)
            return []
