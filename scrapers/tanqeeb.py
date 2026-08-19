"""
Tanqeeb (طنقيب) -- the largest Arab-world job aggregator that still serves bots.

This is the single most valuable addition for Gulf + Egypt coverage. Bayt,
Wuzzuf, Naukrigulf, GulfTalent and Indeed all answer a datacentre IP with
HTTP 403; Tanqeeb returns fully server-rendered HTML across seven regional
subdomains, and -- crucially -- it searches in ARABIC as well as English:

    keywords=دعم فني        -> "Software Support-دعم فني", "اخصائي دعم فنى"
    keywords=مهندس شبكات    -> Network Engineer roles at Vodafone Egypt
    keywords=it support     -> IT Support Engineer at Ninja (Riyadh)
    keywords=odoo           -> Odoo Presales Consultant (Cairo)

Two measured quirks this module has to defend against:

  1. The search parameter is `keywords` (PLURAL). `keyword`, `q`, `search`,
     `query` and `title` are all silently ignored -- the page still returns
     HTTP 200 with 20 cards, just not the ones you asked for. That failure is
     invisible unless you check the results, which is exactly how a scraper
     ends up quietly feeding noise into the pipeline for months.

  2. A zero-result search does NOT return zero cards. It falls back to generic
     recent listings, so searching `voip` yields "Governess" and "Reservation
     Agent". `_is_relevant` therefore requires a card to actually contain a
     token from the query before it is emitted -- otherwise every run would
     inject ~140 unrelated postings into the deduplication store.

Multi-word AND queries ("asterisk pbx") match nothing, so terms are kept short.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

import http_client
from models import JobPost, normalise_text
from scrapers.base import BaseScraper, clean, parse_date, strip_html

log = logging.getLogger(__name__)

SEARCH_URL = "https://{sub}.tanqeeb.com/jobs/search?{query}"

# Card structure verified live 2026-08-19 across all seven subdomains.
SEL_CARD = ".search-job-card"
SEL_TITLE = "h2"
SEL_COMPANY = '[class*="company"]'
SEL_LOCATION = '[class*="location"]'

SUBDOMAIN_COUNTRY = {
    "egypt": "Egypt",
    "saudi": "Saudi Arabia",
    "uae": "United Arab Emirates",
    "qatar": "Qatar",
    "kuwait": "Kuwait",
    "oman": "Oman",
    "bahrain": "Bahrain",
}

# Tokens too generic to prove a card is relevant to its query.
_STOPWORDS = {
    "it", "and", "or", "the", "of", "in", "for", "a", "an",
    "مهندس", "فني", "اخصائي", "أخصائي",
}


class TanqeebScraper(BaseScraper):
    name = "tanqeeb"

    def __init__(self, cfg: dict[str, Any], timeout: int = 25):
        super().__init__(cfg, timeout)
        self.countries = [
            str(c).strip().lower()
            for c in (cfg.get("countries") or ["egypt", "saudi", "uae"])
        ]
        self.terms = [str(t).strip() for t in (cfg.get("terms") or []) if str(t).strip()]
        self.max_per_query = int(cfg.get("max_per_query", 20))
        self.recency_days = int(cfg.get("recency_days", 14))
        # Detail-page fetches per run (one request each) to obtain the full
        # JobPosting description and a real datePosted.
        self.enrich_budget = int(cfg.get("enrich_budget", 18))
        self.profile: dict[str, Any] = {}

    # -- relevance guard ----------------------------------------------------
    @staticmethod
    def _query_tokens(term: str) -> list[str]:
        """Significant tokens the card must echo back to count as a real hit."""
        tokens = [
            t for t in normalise_text(term).split()
            if len(t) >= 3 and t not in _STOPWORDS
        ]
        return tokens or [normalise_text(term)]

    @staticmethod
    def _is_relevant(card_text: str, tokens: list[str]) -> bool:
        """Tanqeeb pads zero-result searches with unrelated recent jobs.

        Requiring one query token in the card is what separates a genuine match
        from that filler. Kept as OR (not AND) so a two-word query still matches
        a card that only names one of the words.
        """
        blob = normalise_text(card_text)
        return any(tok in blob for tok in tokens)

    # -- fetching -----------------------------------------------------------
    def _search(self, sub: str, term: str) -> list[JobPost]:
        base = f"https://{sub}.tanqeeb.com"
        query = urlencode({
            "keywords": term,                 # PLURAL -- see the module docstring
            "search_period": str(self.recency_days),
        })
        html = http_client.get_text(
            SEARCH_URL.format(sub=sub, query=query),
            timeout=self.timeout,
            headers={"Accept-Language": "ar,en-US;q=0.9,en;q=0.8"},
        )
        if not html.strip():
            return []

        soup = BeautifulSoup(html, "lxml")
        cards = soup.select(SEL_CARD)
        if not cards:
            log.debug("[tanqeeb] no cards: %s / %r", sub, term)
            return []

        tokens = self._query_tokens(term)
        country = SUBDOMAIN_COUNTRY.get(sub, sub.title())
        jobs: list[JobPost] = []
        filler = 0

        for card in cards[: self.max_per_query]:
            title_el = card.select_one(SEL_TITLE)
            if not title_el:
                continue
            title = clean(title_el.get_text(" ", strip=True), 300)
            if not title:
                continue

            card_text = card.get_text(" ", strip=True)
            if not self._is_relevant(card_text, tokens):
                filler += 1
                continue

            company_el = card.select_one(SEL_COMPANY)
            loc_el = card.select_one(SEL_LOCATION)
            link_el = card.select_one("a[href]")
            time_el = card.select_one("time, [class*='date']")

            href = link_el.get("href", "") if link_el else ""
            url = urljoin(base, href) if href else ""

            location = clean(loc_el.get_text(" ", strip=True), 200) if loc_el else ""
            # Cards render location as "On-site - Saudi - Riyadh"; keep it but
            # guarantee the country is present for the location scorer.
            if country.lower() not in location.lower():
                location = f"{location} - {country}".strip(" -")

            posted = None
            if time_el:
                posted = parse_date(
                    time_el.get("datetime") or time_el.get_text(strip=True)
                )

            jobs.append(JobPost(
                source=f"tanqeeb:{sub}",
                title=title,
                company=clean(company_el.get_text(" ", strip=True), 200) if company_el else "",
                location=location,
                url=url,
                # Cards carry no description; Gemini works from title + company
                # + location, and the title on Tanqeeb is unusually descriptive.
                description="",
                posted_at=posted,
                raw={"country": country, "term": term, "subdomain": sub},
            ))

        if filler:
            log.debug(
                "[tanqeeb] %s/%r: dropped %d filler card(s) from a zero-result search",
                sub, term, filler,
            )
        return jobs

    # -- enrichment: pull the full description from the detail page ---------
    def _enrich(self, jobs: list[JobPost], profile: dict[str, Any]) -> int:
        """Fetch `JobPosting` JSON-LD for the most promising cards.

        Search cards carry no description and no timestamp, which leaves Gemini
        judging a role from its title alone and the age gate blind. Detail pages
        publish a full schema.org JobPosting block -- structured, stable, and far
        better than scraping the rendered HTML. Budgeted, because it costs one
        request per posting.
        """
        from relevance import score_job

        ranked = sorted(
            (j for j in jobs if j.url),
            key=lambda j: score_job(j, profile)[0],
            reverse=True,
        )
        enriched = 0
        for job in ranked[: self.enrich_budget]:
            html = http_client.get_text(job.url, timeout=self.timeout)
            if not html.strip():
                continue
            payload = self._jobposting_ld(html)
            if not payload:
                continue

            description = strip_html(payload.get("description"))
            if description:
                job.description = clean(description, 5000)
                enriched += 1
            if payload.get("datePosted"):
                job.posted_at = parse_date(payload["datePosted"]) or job.posted_at
            org = payload.get("hiringOrganization") or {}
            if isinstance(org, dict) and org.get("name") and not job.company:
                job.company = clean(str(org["name"]), 200)
        return enriched

    @staticmethod
    def _jobposting_ld(html: str) -> dict[str, Any] | None:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.select('script[type="application/ld+json"]'):
            if not tag.string:
                continue
            try:
                data = json.loads(tag.string)
            except (json.JSONDecodeError, TypeError):
                continue
            for node in data if isinstance(data, list) else [data]:
                if isinstance(node, dict) and node.get("@type") == "JobPosting":
                    return node
        return None

    # -- driver -------------------------------------------------------------
    def collect(self) -> Iterable[JobPost]:
        if not self.terms or not self.countries:
            return []

        jobs: list[JobPost] = []
        dead = 0
        for sub in self.countries:
            found = 0
            for term in self.terms:
                try:
                    batch = self._search(sub, term)
                except Exception as exc:
                    log.warning("[tanqeeb] %s/%r failed: %s", sub, term, exc)
                    continue
                jobs.extend(batch)
                found += len(batch)
            if found == 0:
                dead += 1
            log.info("[tanqeeb] %s -> %d relevant posting(s) (%d total)",
                     sub, found, len(jobs))

        if jobs and self.enrich_budget > 0:
            enriched = self._enrich(jobs, self.profile)
            log.info("[tanqeeb] enriched %d/%d posting(s) with full descriptions",
                     enriched, min(self.enrich_budget, len(jobs)))

        if self.countries and dead == len(self.countries):
            log.error(
                "[tanqeeb] Every subdomain returned zero relevant postings. Either "
                "the card markup changed (update SEL_* in scrapers/tanqeeb.py) or "
                "the `keywords` parameter was renamed again."
            )
        return jobs
