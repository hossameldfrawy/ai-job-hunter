"""
Free, keyless, structured job-board APIs.

Unlike the HTML scrapers these return clean JSON and never break on a markup
change, so they are the stable floor of the ingestion layer. Each adapter maps
one vendor's schema onto `JobPost`; every field shape below was verified live
against the real endpoint on 2026-08-19.

Coverage skews remote/global rather than GCC, which is exactly why they
complement -- rather than replace -- LinkedIn and Telegram.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

import http_client
from models import JobPost
from scrapers.base import BaseScraper, clean, strip_html, parse_date

log = logging.getLogger(__name__)

Adapter = Callable[[dict[str, Any]], list[JobPost]]


def _items(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """Pull the record list out of whichever envelope the vendor used."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


# --------------------------------------------------------------------------
# Adapters -- one per vendor
# --------------------------------------------------------------------------
def _arbeitnow(payload: Any) -> list[JobPost]:
    out = []
    for it in _items(payload, "data"):
        out.append(JobPost(
            source="api:arbeitnow",
            title=clean(it.get("title"), 300),
            company=clean(it.get("company_name"), 200),
            location=clean(it.get("location"), 200) or ("Remote" if it.get("remote") else ""),
            url=it.get("url") or "",
            description=strip_html(it.get("description"))[:4000],
            posted_at=parse_date(it.get("created_at")),
            raw={"tags": it.get("tags")},
        ))
    return out


def _remoteok(payload: Any) -> list[JobPost]:
    out = []
    for it in _items(payload):
        # element 0 of this feed is a legal notice, not a job
        if not it.get("position") and not it.get("title"):
            continue
        out.append(JobPost(
            source="api:remoteok",
            title=clean(it.get("position") or it.get("title"), 300),
            company=clean(it.get("company"), 200),
            location=clean(it.get("location"), 200) or "Remote",
            url=it.get("url") or it.get("apply_url") or "",
            description=strip_html(it.get("description"))[:4000],
            posted_at=parse_date(it.get("epoch") or it.get("date")),
            raw={"tags": it.get("tags")},
        ))
    return out


def _jobicy(payload: Any) -> list[JobPost]:
    out = []
    for it in _items(payload, "jobs"):
        out.append(JobPost(
            source="api:jobicy",
            title=clean(it.get("jobTitle"), 300),
            company=clean(it.get("companyName"), 200),
            location=clean(it.get("jobGeo"), 200) or "Remote",
            url=it.get("url") or "",
            description=strip_html(
                it.get("jobDescription") or it.get("jobExcerpt")
            )[:4000],
            posted_at=parse_date(it.get("pubDate")),
            raw={"level": it.get("jobLevel"), "industry": it.get("jobIndustry")},
        ))
    return out


def _himalayas(payload: Any) -> list[JobPost]:
    out = []
    for it in _items(payload, "jobs"):
        slug = it.get("companySlug") or ""
        url = (
            it.get("applicationLink")
            or it.get("url")
            or it.get("guid")
            or (f"https://himalayas.app/companies/{slug}/jobs" if slug else "")
        )
        locations = it.get("locationRestrictions") or []
        out.append(JobPost(
            source="api:himalayas",
            title=clean(it.get("title"), 300),
            company=clean(it.get("companyName"), 200),
            location=clean(", ".join(map(str, locations)), 200) or "Remote",
            url=url,
            description=strip_html(
                it.get("description") or it.get("excerpt")
            )[:4000],
            posted_at=parse_date(it.get("pubDate") or it.get("publishedDate")),
            raw={"seniority": it.get("seniority")},
        ))
    return out


def _remotive(payload: Any) -> list[JobPost]:
    out = []
    for it in _items(payload, "jobs"):
        out.append(JobPost(
            source="api:remotive",
            title=clean(it.get("title"), 300),
            company=clean(it.get("company_name"), 200),
            location=clean(it.get("candidate_required_location"), 200) or "Remote",
            url=it.get("url") or "",
            description=strip_html(it.get("description"))[:4000],
            posted_at=parse_date(it.get("publication_date")),
            raw={"category": it.get("category")},
        ))
    return out


def _themuse(payload: Any) -> list[JobPost]:
    out = []
    for it in _items(payload, "results"):
        company = (it.get("company") or {}).get("name", "")
        locations = [
            loc.get("name", "") for loc in (it.get("locations") or []) if isinstance(loc, dict)
        ]
        refs = it.get("refs") or {}
        out.append(JobPost(
            source="api:themuse",
            title=clean(it.get("name"), 300),
            company=clean(company, 200),
            location=clean(", ".join(filter(None, locations)), 200),
            url=refs.get("landing_page") or "",
            description=strip_html(it.get("contents"))[:4000],
            posted_at=parse_date(it.get("publication_date")),
            raw={"levels": [l.get("name") for l in (it.get("levels") or []) if isinstance(l, dict)]},
        ))
    return out


# name -> (endpoint, adapter)
REGISTRY: dict[str, tuple[str, Adapter]] = {
    "arbeitnow": ("https://www.arbeitnow.com/api/job-board-api", _arbeitnow),
    "remoteok": ("https://remoteok.com/api", _remoteok),
    "jobicy": ("https://jobicy.com/api/v2/remote-jobs?count=50", _jobicy),
    "himalayas": ("https://himalayas.app/jobs/api?limit=50", _himalayas),
    "remotive": ("https://remotive.com/api/remote-jobs?limit=50", _remotive),
    "themuse": ("https://www.themuse.com/api/public/jobs?page=1", _themuse),
}


class JobApiScraper(BaseScraper):
    name = "job_apis"

    def collect(self) -> Iterable[JobPost]:
        wanted = self.cfg.get("sources") or {}
        jobs: list[JobPost] = []

        for key, (url, adapter) in REGISTRY.items():
            if not wanted.get(key, False):
                continue
            payload = http_client.get_json(url, timeout=self.timeout)
            if payload is None:
                log.info("[job_apis] %s unreachable this run -- skipping.", key)
                continue
            try:
                batch = adapter(payload)
            except Exception as exc:
                log.warning("[job_apis] %s changed its schema (%s) -- skipping.", key, exc)
                continue
            batch = [j for j in batch if j.title]
            log.info("[job_apis] %s -> %d postings", key, len(batch))
            jobs.extend(batch)

        return jobs
