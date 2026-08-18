"""
Core domain objects shared by every layer of the pipeline.

`JobPost` is the normalised shape every scraper must emit. `Evaluation` is what
Gemini returns. `fingerprint` is the identity used for deduplication and is the
single most important function in this file -- if it is unstable, the user gets
the same job on WhatsApp twice; if it is too aggressive, real jobs vanish.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# Tracking parameters that change per-request and would otherwise defeat
# URL-based deduplication.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "trk", "trackingId", "refId", "position", "pageNum", "originalSubdomain",
    "src", "ref", "source", "gclid", "fbclid", "mc_cid", "mc_eid", "eBP",
    "savedSearchId", "originToLandingJobPostings", "lipi", "licu",
}

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9؀-ۿ ]+")

# Boilerplate that recruiters bolt onto every title and that breaks matching.
_TITLE_NOISE = re.compile(
    r"\b(urgent(ly)?|hiring|immediate joiner[s]?|apply now|we are hiring|"
    r"required|wanted|vacancy|vacancies|job opening|opportunity|new)\b",
    re.I,
)


def normalise_text(value: str | None) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace.

    Keeps the Arabic block (U+0600-U+06FF) intact because a large share of Gulf
    and Egyptian postings are Arabic and would otherwise normalise to "".
    """
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.casefold()
    value = _NON_ALNUM.sub(" ", value)
    return _WS.sub(" ", value).strip()


def canonical_url(url: str | None) -> str:
    """Strip tracking noise so the same posting yields one stable URL."""
    if not url:
        return ""
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme:
        return url

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k not in _TRACKING_PARAMS
    ]
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query), ""))


def _clean_title(title: str) -> str:
    return normalise_text(_TITLE_NOISE.sub(" ", title or ""))


def fingerprint(company: str, title: str, location: str, url: str = "") -> str:
    """Stable identity for a posting.

    Content-based rather than URL-based: the same role syndicated to Bayt,
    LinkedIn and a Telegram channel must collapse to ONE alert. Falls back to
    the canonical URL only when there is no usable company/title pair.
    """
    ctitle = _clean_title(title)
    ccompany = normalise_text(company)
    # Country-level granularity: "Dubai, Dubai, UAE" and "Dubai, UAE" are the
    # same place, so only the most significant token is used.
    cloc = normalise_text(location).split(" ")
    cloc_key = " ".join(sorted(set(cloc)))[:60]

    if ctitle and ccompany:
        basis = f"{ccompany}|{ctitle}|{cloc_key}"
    elif ctitle:
        basis = f"|{ctitle}|{cloc_key}"
    else:
        basis = canonical_url(url) or f"{ccompany}|{cloc_key}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str:
    return (dt or utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class JobPost:
    """One raw posting, as emitted by a scraper. Pre-AI, pre-scoring."""

    source: str                       # e.g. "linkedin", "telegram:jobsgulf"
    title: str = ""
    company: str = ""
    location: str = ""
    url: str = ""
    description: str = ""
    posted_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    # populated by the pipeline
    prefilter_score: int = 0

    def __post_init__(self) -> None:
        self.title = (self.title or "").strip()[:400]
        self.company = (self.company or "").strip()[:200]
        self.location = (self.location or "").strip()[:200]
        self.url = canonical_url(self.url)
        self.description = (self.description or "").strip()

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.company, self.title, self.location, self.url)

    @property
    def url_hash(self) -> str:
        cu = canonical_url(self.url)
        return hashlib.sha256(cu.encode("utf-8")).hexdigest()[:32] if cu else ""

    @property
    def age_days(self) -> float | None:
        if not self.posted_at:
            return None
        delta = utc_now() - self.posted_at.astimezone(timezone.utc)
        return max(0.0, delta.total_seconds() / 86400.0)

    def searchable_text(self) -> str:
        """Everything a lexical filter should look at."""
        return " ".join(
            filter(None, [self.title, self.company, self.location, self.description])
        )

    def to_prompt_block(self, index: int, max_desc: int = 1400) -> str:
        """Compact representation handed to Gemini. Token budget matters."""
        desc = _WS.sub(" ", self.description or "")[:max_desc]
        posted = iso(self.posted_at) if self.posted_at else "unknown"
        return (
            f"### JOB {index}\n"
            f"source: {self.source}\n"
            f"title: {self.title or 'unknown'}\n"
            f"company: {self.company or 'unknown'}\n"
            f"location: {self.location or 'unknown'}\n"
            f"url: {self.url or 'none'}\n"
            f"posted: {posted}\n"
            f"description: {desc or '(none provided)'}\n"
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["posted_at"] = iso(self.posted_at) if self.posted_at else None
        d.pop("raw", None)
        d["fingerprint"] = self.fingerprint
        return d


@dataclass(slots=True)
class Evaluation:
    """Gemini's verdict on one JobPost."""

    fingerprint: str
    company_name: str = "Unknown"
    role_title: str = "Unknown"
    location: str = "Unknown"
    match_score: int = 0
    source_platform: str = "unknown"
    direct_link: str = ""
    why_matched: str = ""
    skill_gaps: list[str] = field(default_factory=list)
    model: str = ""
    error: str = ""

    @classmethod
    def from_gemini(
        cls, payload: dict[str, Any], job: JobPost, model: str
    ) -> "Evaluation":
        """Build from Gemini JSON, trusting the job record over the model for
        facts the model tends to hallucinate (links, source)."""

        def _s(key: str, fallback: str = "") -> str:
            val = payload.get(key)
            return str(val).strip() if val not in (None, "") else fallback

        try:
            score = int(float(payload.get("match_score", 0)))
        except (TypeError, ValueError):
            score = 0

        gaps = payload.get("skill_gaps") or []
        if isinstance(gaps, str):
            gaps = [g.strip() for g in gaps.split(",") if g.strip()]
        gaps = [str(g).strip() for g in gaps if str(g).strip()][:6]

        return cls(
            fingerprint=job.fingerprint,
            company_name=_s("company_name", job.company or "Unknown"),
            role_title=_s("role_title", job.title or "Unknown"),
            location=_s("location", job.location or "Unknown"),
            match_score=max(0, min(100, score)),
            # The model must never invent these two:
            source_platform=job.source,
            direct_link=job.url or _s("direct_link"),
            why_matched=_s("why_matched")[:600],
            skill_gaps=gaps,
            model=model,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
