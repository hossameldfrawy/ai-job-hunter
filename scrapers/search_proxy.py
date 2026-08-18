"""
Search-engine proxy for job boards that refuse direct scraping.

Bayt, GulfTalent, Naukrigulf, Wuzzuf and Indeed are the GCC-native boards this
system most wants to read -- and every one of them answers a datacentre IP with
HTTP 403. They are, however, indexed by Google, and Google News exposes a
keyless RSS endpoint that honours the `site:` operator.

Measured limitations, stated plainly rather than papered over:
  * Google News wraps every link in an opaque `news.google.com/rss/articles/...`
    redirect. It cannot be decoded offline and does not resolve server-side --
    it resolves only in a real browser. The link IS tappable from WhatsApp and
    does land on the posting, but it is not a clean canonical URL.
  * Precision is lower than a real job API: results include board *listing*
    pages and candidate profiles alongside actual vacancies. `_LISTING_NOISE`
    below strips the worst of that, and Gemini rejects the rest.
  * Bing's RSS endpoint ignores `site:` entirely (verified: it returns the
    Microsoft home page for `site:bayt.com voip`), and DuckDuckGo answers
    HTTP 202 bot-challenges. Neither is usable, so neither is wired up.

Treat this as breadth, not precision. It is the only free path to those boards.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable
from urllib.parse import quote_plus

import feedparser

import http_client
from models import JobPost
from scrapers.base import BaseScraper, clean, strip_html, parse_date

log = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)

# Titles that are aggregate listing pages or CV profiles, not real vacancies.
_LISTING_NOISE = re.compile(
    r"(^\s*\d[\d,]*\s+\w[\w\- ]*\s+jobs\b"      # "774 odoo-erp Jobs in Egypt"
    r"|\bjobs?\s+in\s+.+\(\w+\s+\d{4}\)"        # "... Jobs in Beirut (Jul 2026)"
    r"|\bjob\s+vacancies\s+in\b"
    r"|\bsalary\b|\bsalaries\b"
    r"|\bcv\b|\bresume\b|\bprofile\b"
    r"|–\s*\w+\s+at\s+\w+"                 # "Name - role at company" (profile)
    r"|\bcompanies\s+hiring\b|\bcareer\s+advice\b)",
    re.I,
)


class SearchProxyScraper(BaseScraper):
    name = "search_proxy"

    def _query(self, site: str, term: str) -> list[JobPost]:
        raw_query = f'site:{site} "{term}"'
        url = GOOGLE_NEWS_RSS.format(query=quote_plus(raw_query))
        body = http_client.get_text(url, timeout=self.timeout)
        if not body.strip():
            return []

        parsed = feedparser.parse(body)
        limit = int(self.cfg.get("max_items_per_query", 25))
        jobs: list[JobPost] = []

        for entry in parsed.entries[:limit]:
            title = clean(entry.get("title"), 300)
            if not title or _LISTING_NOISE.search(title):
                continue

            # Google News appends " - Publisher"; strip it for cleaner titles.
            display = re.sub(r"\s+-\s+[^-]{2,40}$", "", title).strip()

            jobs.append(JobPost(
                source=f"search:{site}",
                title=display or title,
                company="",       # Gemini infers this from the title/snippet
                location="",
                url=entry.get("link") or "",
                description=strip_html(entry.get("summary"))[:1500],
                posted_at=parse_date(entry.get("published")),
                raw={"site": site, "term": term, "via": "google_news_rss"},
            ))
        return jobs

    def collect(self) -> Iterable[JobPost]:
        engines = [str(e).lower() for e in (self.cfg.get("engines") or ["google_news"])]
        if "google_news" not in engines:
            log.info("[search_proxy] google_news not enabled; nothing else is usable.")
            return []

        jobs: list[JobPost] = []
        for block in self.cfg.get("site_queries") or []:
            site = str(block.get("site", "")).strip()
            if not site:
                continue
            for term in block.get("terms") or []:
                try:
                    batch = self._query(site, str(term))
                except Exception as exc:
                    log.warning("[search_proxy] %s/%r failed: %s", site, term, exc)
                    continue
                jobs.extend(batch)
            log.info("[search_proxy] %s -> %d cumulative results", site, len(jobs))
        return jobs
