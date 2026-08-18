"""
Facebook public recruitment posts -- best effort, and honest about it.

What was actually tested (2026-08-19):
  * `facebook.com/<page>/posts` -> login wall for anonymous clients
  * `mbasic.facebook.com`       -> login wall; the old text-only bypass is gone
  * Page RSS feeds              -> removed by Facebook years ago
  * Graph API                   -> requires an app review + page access token
                                   the user does not have

So there is NO free, credential-less way to read Facebook recruitment groups.
Rather than ship a scraper that silently returns nothing forever, this module
does two honest things:

  1. DEFAULT PATH -- finds publicly-indexed Facebook job posts through the same
     Google News RSS proxy used for the 403-blocked boards. Low volume, but
     real, and it costs nothing.

  2. AUTHENTICATED PATH -- if (and only if) you set the FACEBOOK_COOKIE secret
     to your own `c_user=...; xs=...` cookie string, it reads mbasic pages
     directly, which is dramatically more productive.

     Note this uses YOUR account and is subject to Facebook's automation rules;
     it is opt-in for that reason and is never enabled by default.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable
from urllib.parse import quote_plus

import feedparser
from bs4 import BeautifulSoup

import http_client
from config import settings
from models import JobPost
from scrapers.base import BaseScraper, clean, strip_html, parse_date

log = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)
MBASIC = "https://mbasic.facebook.com/{page}"

_JOBBY = re.compile(
    r"(hiring|vacanc|we are looking|apply|job|position|opening|"
    r"مطلوب|وظيف|وظائف|فرصة)",
    re.I,
)


class FacebookScraper(BaseScraper):
    name = "facebook"

    # -- path 1: publicly indexed posts (no credentials) --------------------
    def _via_search(self) -> list[JobPost]:
        jobs: list[JobPost] = []
        for term in self.cfg.get("search_terms") or []:
            query = f'site:facebook.com "{term}"'
            body = http_client.get_text(
                GOOGLE_NEWS_RSS.format(query=quote_plus(query)), timeout=self.timeout
            )
            if not body.strip():
                continue
            for entry in feedparser.parse(body).entries[:15]:
                title = clean(entry.get("title"), 300)
                if not title:
                    continue
                jobs.append(JobPost(
                    source="facebook:indexed",
                    title=re.sub(r"\s+-\s+Facebook\s*$", "", title, flags=re.I),
                    url=entry.get("link") or "",
                    description=strip_html(entry.get("summary"))[:1500],
                    posted_at=parse_date(entry.get("published")),
                    raw={"term": term, "via": "google_news_rss"},
                ))
        return jobs

    # -- path 2: authenticated mbasic (opt-in, user's own cookie) -----------
    def _via_cookie(self) -> list[JobPost]:
        cookie = settings.facebook_cookie
        pages = [str(p).strip() for p in (self.cfg.get("pages") or []) if str(p).strip()]
        if not cookie or not pages:
            return []

        jobs: list[JobPost] = []
        for page in pages:
            html = http_client.get_text(
                MBASIC.format(page=page.lstrip("/")),
                timeout=self.timeout,
                headers={"Cookie": cookie},
            )
            if not html.strip() or "login" in html[:2000].lower():
                log.warning(
                    "[facebook] mbasic returned a login wall for %r -- the "
                    "FACEBOOK_COOKIE secret is missing or expired.", page,
                )
                continue

            soup = BeautifulSoup(html, "lxml")
            for story in soup.select("div[data-ft], article, #m_story_permalink_view div"):
                text = clean(story.get_text("\n", strip=True), 4000)
                if len(text) < 60 or not _JOBBY.search(text):
                    continue
                link_el = story.select_one('a[href*="story.php"], a[href*="/posts/"]')
                href = link_el.get("href", "") if link_el else ""
                if href.startswith("/"):
                    href = "https://www.facebook.com" + href
                jobs.append(JobPost(
                    source=f"facebook:{page}",
                    title=text.splitlines()[0][:180],
                    url=href,
                    description=text,
                    raw={"page": page, "via": "mbasic"},
                ))
        return jobs

    def collect(self) -> Iterable[JobPost]:
        jobs = list(self._via_search())
        authed = self._via_cookie()
        if authed:
            log.info("[facebook] %d posts via authenticated mbasic", len(authed))
            jobs.extend(authed)
        elif not settings.facebook_cookie:
            log.info(
                "[facebook] Running in indexed-only mode (%d results). Facebook "
                "blocks anonymous scraping; set the FACEBOOK_COOKIE secret to "
                "read pages directly.", len(jobs),
            )
        return jobs
