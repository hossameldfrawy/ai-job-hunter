"""
Generic RSS/Atom job-feed ingestion.

Deliberately dumb and completely generic: point it at any feed URL in
config.yml and it will normalise the entries. Feeds are the most durable
integration surface on the web -- they rarely change shape and rarely block
bots -- so this is the lowest-maintenance source in the system.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

import feedparser

import http_client
from models import JobPost
from scrapers.base import BaseScraper, clean, strip_html, parse_date

log = logging.getLogger(__name__)

# Feeds vary wildly in where they hide the company name.
_COMPANY_KEYS = ("author", "dc_creator", "creator", "publisher", "company")


def _entry_company(entry: Any, title: str) -> str:
    for key in _COMPANY_KEYS:
        value = entry.get(key) if hasattr(entry, "get") else None
        if value:
            return clean(str(value), 200)
    # WeWorkRemotely encodes "Company: Role" in the title.
    if ":" in title:
        head = title.split(":", 1)[0].strip()
        if 2 < len(head) < 60:
            return head
    return ""


class RssScraper(BaseScraper):
    name = "rss"

    def collect(self) -> Iterable[JobPost]:
        feeds = self.cfg.get("feeds") or []
        jobs: list[JobPost] = []

        for feed in feeds:
            name = str(feed.get("name") or "feed")
            url = str(feed.get("url") or "").strip()
            if not url:
                continue

            # Fetch through the shared client so throttling and the circuit
            # breaker apply, then hand the bytes to feedparser.
            body = http_client.get_text(url, timeout=self.timeout)
            if not body.strip():
                log.info("[rss] %s unreachable -- skipping.", name)
                continue

            parsed = feedparser.parse(body)
            if parsed.bozo and not parsed.entries:
                log.info("[rss] %s returned unparseable XML -- skipping.", name)
                continue

            for entry in parsed.entries:
                title = clean(entry.get("title"), 300)
                if not title:
                    continue
                summary = (
                    entry.get("summary")
                    or entry.get("description")
                    or (entry.get("content") or [{}])[0].get("value", "")
                )
                jobs.append(JobPost(
                    source=f"rss:{name}",
                    title=title,
                    company=_entry_company(entry, title),
                    location=clean(entry.get("location"), 200),
                    url=entry.get("link") or "",
                    description=strip_html(summary)[:4000],
                    posted_at=parse_date(
                        entry.get("published") or entry.get("updated")
                    ),
                    raw={"feed": name},
                ))
            log.info("[rss] %s -> %d entries", name, len(parsed.entries))

        return jobs
