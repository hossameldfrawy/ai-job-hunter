"""
Scraper contract + shared parsing helpers.

Every source subclasses `BaseScraper` and implements `collect()`. The base class
guarantees that a broken source can never take down a run: exceptions are
caught, timed, counted and reported, and the pipeline simply proceeds with the
sources that did work.
"""

from __future__ import annotations

import html
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from models import JobPost, utc_now

log = logging.getLogger(__name__)

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
URL_RE = re.compile(r"https?://[^\s<>\"'\)\]}]+", re.I)

# Relative timestamps used by Telegram/RSS ("3 days ago", "منذ يومين").
_REL_RE = re.compile(
    r"(\d+)\s*(minute|min|hour|hr|day|week|month)s?\s*ago", re.I
)


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = _TAG.sub(" ", str(value))
    text = html.unescape(text)
    return _WS.sub(" ", text).strip()


def clean(value: str | None, limit: int = 6000) -> str:
    if not value:
        return ""
    return _WS.sub(" ", html.unescape(str(value))).strip()[:limit]


def parse_date(value: Any) -> datetime | None:
    """Best-effort timestamp parsing across every format our sources emit."""
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    # Unix epoch (seconds or milliseconds)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # milliseconds
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    if text.isdigit():
        return parse_date(int(text))

    rel = _REL_RE.search(text)
    if rel:
        qty = int(rel.group(1))
        unit = rel.group(2).lower()
        mult = {
            "minute": 1 / 1440, "min": 1 / 1440,
            "hour": 1 / 24, "hr": 1 / 24,
            "day": 1, "week": 7, "month": 30,
        }.get(unit, 0)
        return utc_now() - timedelta(days=qty * mult)

    normalised = text.replace("Z", "+00:00")
    for fmt in (
        None,  # ISO-8601 via fromisoformat
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d, %Y",
    ):
        try:
            dt = (
                datetime.fromisoformat(normalised)
                if fmt is None
                else datetime.strptime(text, fmt)
            )
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def first_url(text: str) -> str:
    m = URL_RE.search(text or "")
    if not m:
        return ""
    # Trailing punctuation frequently gets swept into the match.
    return m.group(0).rstrip(".,;:!?)»،")


@dataclass(slots=True)
class ScrapeResult:
    name: str
    jobs: list[JobPost] = field(default_factory=list)
    ok: bool = True
    error: str = ""
    duration_s: float = 0.0

    @property
    def count(self) -> int:
        return len(self.jobs)


class BaseScraper(ABC):
    """Base class for every ingestion source."""

    #: short, stable identifier used in logs, the DB and WhatsApp alerts
    name: str = "base"

    def __init__(self, cfg: dict[str, Any], timeout: int = 25):
        self.cfg = cfg or {}
        self.timeout = timeout

    @abstractmethod
    def collect(self) -> Iterable[JobPost]:
        """Yield raw postings. May raise -- `run()` contains the blast radius."""

    def run(self) -> ScrapeResult:
        started = time.monotonic()
        try:
            jobs = [j for j in self.collect() if j and (j.title or j.description)]
            elapsed = time.monotonic() - started
            log.info("[%s] %d posting(s) in %.1fs", self.name, len(jobs), elapsed)
            return ScrapeResult(self.name, jobs, True, "", elapsed)
        except Exception as exc:
            elapsed = time.monotonic() - started
            log.warning(
                "[%s] FAILED after %.1fs -- %s: %s",
                self.name, elapsed, type(exc).__name__, exc,
            )
            return ScrapeResult(
                self.name, [], False, f"{type(exc).__name__}: {exc}"[:300], elapsed
            )
