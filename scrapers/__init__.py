"""
Scraper registry + concurrent fan-out.

`build_scrapers()` turns config.yml into live scraper objects; `run_all()`
executes them in parallel and returns both the postings and a per-source
health report. One dead source never blocks the others: each runs inside
`BaseScraper.run()`, which catches everything.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from models import JobPost
from scrapers.base import BaseScraper, ScrapeResult
from scrapers.facebook import FacebookScraper
from scrapers.job_apis import JobApiScraper
from scrapers.linkedin import LinkedInScraper
from scrapers.rss_feeds import RssScraper
from scrapers.search_proxy import SearchProxyScraper
from scrapers.talent import TalentScraper
from scrapers.tanqeeb import TanqeebScraper
from scrapers.telegram_user_client import TelegramUserClientScraper
from scrapers.telegram_web import TelegramWebScraper

log = logging.getLogger(__name__)

__all__ = [
    "BaseScraper", "ScrapeResult", "build_scrapers", "run_all",
    "LinkedInScraper", "TelegramWebScraper", "TelegramUserClientScraper",
    "TalentScraper", "TanqeebScraper", "JobApiScraper", "RssScraper",
    "SearchProxyScraper",
    "FacebookScraper",
]

# config.yml key -> scraper class
REGISTRY: dict[str, type[BaseScraper]] = {
    "linkedin": LinkedInScraper,
    "telegram": TelegramWebScraper,
    "telegram_user": TelegramUserClientScraper,
    "talent": TalentScraper,
    "tanqeeb": TanqeebScraper,
    "job_apis": JobApiScraper,
    "search_proxy": SearchProxyScraper,
    "rss": RssScraper,
    "facebook": FacebookScraper,
}


def build_scrapers(settings: Any, db: Any = None) -> list[BaseScraper]:
    """Instantiate every source enabled in config.yml.

    `db` is optional and only used by sources that keep a cursor between runs
    (the Telegram user client stores a last-read message id per chat, so it
    never re-reads the same history twice).
    """
    timeout = settings.http_timeout
    profile = settings.profile
    built: list[BaseScraper] = []

    for key, cls in REGISTRY.items():
        if not settings.source_enabled(key):
            log.debug("Source %r disabled.", key)
            continue
        cfg = settings.source(key)
        try:
            if cls is TanqeebScraper:
                scraper = cls(cfg, timeout)
                # Enrichment ranks candidates before spending detail requests.
                scraper.profile = profile
                built.append(scraper)
            elif cls is LinkedInScraper:
                # Ranks cards against the profile before spending requests on
                # full descriptions, so it needs the profile block.
                built.append(cls(cfg, timeout, profile=profile))
            elif cls is TelegramUserClientScraper:
                if not settings.telegram_ready:
                    log.info(
                        "Source 'telegram_user' is enabled but not authorised yet "
                        "-- run `python auth_telegram.py` to unlock your private "
                        "groups. Skipping it for now."
                    )
                    continue
                built.append(cls(cfg, timeout, db=db))
            else:
                built.append(cls(cfg, timeout))
        except Exception as exc:
            log.error("Could not construct scraper %r: %s", key, exc)

    log.info("Active sources: %s", ", ".join(s.name for s in built) or "(none)")
    return built


def run_all(
    scrapers: list[BaseScraper], max_workers: int = 8
) -> tuple[list[JobPost], list[ScrapeResult]]:
    """Run every scraper concurrently. Returns (all_postings, per_source_report)."""
    if not scrapers:
        return [], []

    jobs: list[JobPost] = []
    results: list[ScrapeResult] = []

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {pool.submit(s.run): s for s in scrapers}
        for future in as_completed(futures):
            scraper = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # BaseScraper.run should prevent this
                log.error("[%s] escaped its error boundary: %s", scraper.name, exc)
                result = ScrapeResult(scraper.name, [], False, str(exc)[:300], 0.0)
            results.append(result)
            jobs.extend(result.jobs)

    ok = sum(1 for r in results if r.ok)
    log.info(
        "Ingestion complete: %d posting(s) from %d/%d healthy sources.",
        len(jobs), ok, len(results),
    )
    return jobs, results
