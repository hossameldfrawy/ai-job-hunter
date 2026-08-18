"""
LinkedIn ingestion via the public *guest* endpoints.

LinkedIn serves an unauthenticated, server-rendered job feed to logged-out
visitors -- the same HTML a person sees before signing in. No login, no cookie,
no credential, no ToS-gated private API:

    /jobs-guest/jobs/api/seeMoreJobPostings/search   -> paginated result cards
    /jobs-guest/jobs/api/jobPosting/{id}             -> full description

This is the highest-signal source in the system: it returns fresh, structured,
GCC-targeted postings with company, location and timestamp already parsed.

Two-phase by design: phase one pulls cheap list cards for every query, phase two
spends one extra request each on only the highest-scoring handful to fetch full
descriptions -- which is what lets Gemini produce a specific `why_matched`
instead of guessing from a job title.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable
from urllib.parse import urlencode

from bs4 import BeautifulSoup

import http_client
from models import JobPost
from relevance import score_job
from scrapers.base import BaseScraper, clean, parse_date

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

_JOB_ID_RE = re.compile(r"(\d{8,})")

# Requests spent on full descriptions per run. Each is ~1s; this caps the
# scraper's wall-clock contribution while still enriching what matters.
DEFAULT_ENRICH_BUDGET = 18


class LinkedInScraper(BaseScraper):
    name = "linkedin"

    def __init__(self, cfg: dict[str, Any], timeout: int = 25,
                 profile: dict[str, Any] | None = None):
        super().__init__(cfg, timeout)
        self.profile = profile or {}
        self.enrich_budget = int(cfg.get("enrich_budget", DEFAULT_ENRICH_BUDGET))

    # -- phase 1: list cards ------------------------------------------------
    def _search(self, keywords: str, location: str, start: int) -> list[JobPost]:
        params = {
            "keywords": keywords,
            "location": location,
            "start": start,
            "f_TPR": f"r{int(self.cfg.get('recency_seconds', 604800))}",
            "sortBy": "DD",  # date descending -- freshest first
        }
        url = f"{SEARCH_URL}?{urlencode(params)}"
        html = http_client.get_text(
            url,
            timeout=self.timeout,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Referer": "https://www.linkedin.com/jobs",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if not html.strip():
            return []

        soup = BeautifulSoup(html, "lxml")
        jobs: list[JobPost] = []
        for card in soup.select("li"):
            job = self._parse_card(card, keywords, location)
            if job:
                jobs.append(job)
        return jobs

    def _parse_card(self, card: Any, keywords: str, location: str) -> JobPost | None:
        title_el = card.select_one("h3")
        if not title_el:
            return None
        title = clean(title_el.get_text(" ", strip=True), 300)
        if not title:
            return None

        company_el = card.select_one("h4")
        loc_el = card.select_one(".job-search-card__location")
        time_el = card.select_one("time")
        link_el = card.select_one("a[href]")

        raw_url = link_el.get("href", "") if link_el else ""
        url = raw_url.split("?")[0].strip()

        posted = None
        if time_el:
            posted = parse_date(
                time_el.get("datetime") or time_el.get_text(strip=True)
            )

        return JobPost(
            source="linkedin",
            title=title,
            company=clean(company_el.get_text(" ", strip=True), 200) if company_el else "",
            location=clean(loc_el.get_text(" ", strip=True), 200) if loc_el else location,
            url=url,
            description="",
            posted_at=posted,
            raw={"job_id": self._job_id(card, url), "query": keywords},
        )

    @staticmethod
    def _job_id(card: Any, url: str) -> str:
        urn = card.get("data-entity-urn") or ""
        if not urn:
            inner = card.select_one("[data-entity-urn]")
            urn = inner.get("data-entity-urn", "") if inner else ""
        for candidate in (urn, url):
            m = _JOB_ID_RE.search(candidate or "")
            if m:
                return m.group(1)
        return ""

    # -- phase 2: full descriptions for the most promising cards ------------
    def _enrich(self, jobs: list[JobPost]) -> None:
        ranked = sorted(
            (j for j in jobs if j.raw.get("job_id")),
            key=lambda j: score_job(j, self.profile)[0],
            reverse=True,
        )
        for job in ranked[: self.enrich_budget]:
            html = http_client.get_text(
                DETAIL_URL.format(job_id=job.raw["job_id"]), timeout=self.timeout
            )
            if not html.strip():
                continue
            soup = BeautifulSoup(html, "lxml")
            body = soup.select_one(
                ".description__text, .show-more-less-html__markup, .description"
            )
            parts: list[str] = []
            if body:
                parts.append(body.get_text(" ", strip=True))
            criteria = [
                c.get_text(" ", strip=True)
                for c in soup.select(".description__job-criteria-item")
            ]
            if criteria:
                parts.append(" | ".join(criteria))
            if parts:
                job.description = clean(" ".join(parts), 5000)

    # -- driver -------------------------------------------------------------
    def collect(self) -> Iterable[JobPost]:
        queries = self.cfg.get("queries") or []
        pages = max(1, int(self.cfg.get("pages_per_query", 2)))
        collected: list[JobPost] = []
        seen_urls: set[str] = set()

        for query in queries:
            keywords = str(query.get("keywords", "")).strip()
            if not keywords:
                continue
            for location in query.get("locations") or [""]:
                for page in range(pages):
                    batch = self._search(keywords, str(location), page * 10)
                    if not batch:
                        break  # exhausted this query
                    for job in batch:
                        key = job.url or f"{job.title}|{job.company}"
                        if key in seen_urls:
                            continue
                        seen_urls.add(key)
                        collected.append(job)

        log.info("[linkedin] %d unique cards; enriching top %d with descriptions",
                 len(collected), min(self.enrich_budget, len(collected)))
        if collected:
            self._enrich(collected)
        return collected
