"""
talent.com regional job boards (ae / sa / qa / kw / om / bh / eg ...).

This is the most productive *GCC-native* source that still serves clean,
server-rendered HTML to a plain HTTP client. Bayt, GulfTalent, Naukrigulf,
Wuzzuf and Indeed all answer a datacentre IP with HTTP 403; talent.com does
not, and it aggregates a large share of the same postings.

Implementation note: talent.com ships CSS-module class names whose hash suffix
changes on every front-end deploy (`JobCard_title__X32Qk`). Matching those
verbatim would silently break within weeks, so every selector here matches on
the STABLE prefix (`[class*="JobCard_title"]`) instead.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

import http_client
from models import JobPost
from scrapers.base import BaseScraper, clean, parse_date

log = logging.getLogger(__name__)

# Stable prefixes of talent.com's hashed CSS-module classes.
SEL_CARD = '[class*="JobCard_card"]'
SEL_TITLE = '[class*="JobCard_title"]'
SEL_COMPANY = '[class*="JobCard_company"]'
SEL_LOCATION = '[class*="JobCard_location"]'
SEL_SNIPPET = '[class*="JobCard_snippet"]'

COUNTRY_NAMES = {
    "ae": "United Arab Emirates",
    "sa": "Saudi Arabia",
    "qa": "Qatar",
    "kw": "Kuwait",
    "om": "Oman",
    "bh": "Bahrain",
    "eg": "Egypt",
}


class TalentScraper(BaseScraper):
    name = "talent"

    def __init__(self, cfg: dict[str, Any], timeout: int = 25):
        super().__init__(cfg, timeout)
        self.countries = [
            str(c).strip().lower() for c in (cfg.get("countries") or ["ae"])
        ]
        self.terms = [str(t).strip() for t in (cfg.get("terms") or []) if str(t).strip()]
        self.max_per_query = int(cfg.get("max_per_query", 20))

    def _search(self, country: str, term: str) -> list[JobPost]:
        base = f"https://{country}.talent.com"
        url = f"{base}/jobs?{urlencode({'k': term, 'l': ''})}"
        html = http_client.get_text(url, timeout=self.timeout)
        if not html.strip():
            return []

        soup = BeautifulSoup(html, "lxml")
        cards = soup.select(SEL_CARD)
        if not cards:
            # Usually just a genuine zero-result search. It only means the
            # markup moved if EVERY term comes back empty for this country --
            # which `collect()` checks and reports.
            log.debug("[talent] no results: %s.talent.com %r", country, term)
            return []

        jobs: list[JobPost] = []
        for card in cards[: self.max_per_query]:
            title_el = card.select_one(SEL_TITLE)
            if not title_el:
                continue
            title = clean(title_el.get_text(" ", strip=True), 300)
            if not title:
                continue

            company_el = card.select_one(SEL_COMPANY)
            loc_el = card.select_one(SEL_LOCATION)
            snip_el = card.select_one(SEL_SNIPPET)
            time_el = card.select_one("time")
            link_el = card.select_one("a[href]")

            href = link_el.get("href", "") if link_el else ""
            url_abs = urljoin(base, href) if href else ""

            posted = None
            if time_el:
                posted = parse_date(
                    time_el.get("datetime") or time_el.get_text(strip=True)
                )

            location = clean(loc_el.get_text(" ", strip=True), 200) if loc_el else ""
            if not location:
                location = COUNTRY_NAMES.get(country, country.upper())

            jobs.append(JobPost(
                source=f"talent:{country}",
                title=title,
                company=clean(company_el.get_text(" ", strip=True), 200) if company_el else "",
                location=location,
                url=url_abs,
                description=clean(snip_el.get_text(" ", strip=True), 2500) if snip_el else "",
                posted_at=posted,
                raw={"country": country, "term": term},
            ))
        return jobs

    def collect(self) -> Iterable[JobPost]:
        if not self.terms:
            return []
        jobs: list[JobPost] = []
        dead_countries = 0

        for country in self.countries:
            found_here = 0
            for term in self.terms:
                try:
                    batch = self._search(country, term)
                except Exception as exc:
                    log.warning("[talent] %s/%r failed: %s", country, term, exc)
                    continue
                jobs.extend(batch)
                found_here += len(batch)
            if found_here == 0:
                dead_countries += 1
            log.info("[talent] %s -> %d postings (%d total)",
                     country, found_here, len(jobs))

        # Every term empty in every country means the selectors broke, not that
        # the Gulf ran out of jobs. Say so loudly -- silence would look healthy.
        if self.countries and dead_countries == len(self.countries):
            log.error(
                "[talent] ALL %d countries returned zero cards. talent.com most "
                "likely changed its markup; update the SEL_* prefixes in "
                "scrapers/talent.py.", len(self.countries),
            )
        return jobs
