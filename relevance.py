"""
Cheap lexical pre-filter -- the gate that sits in front of Gemini.

Aggregators return thousands of postings, the overwhelming majority of which
are irrelevant (nursing, construction, sales). Sending all of them to Gemini
would exhaust the free-tier quota within minutes and cost real latency.

This module scores a posting using nothing but string matching, and only what
clears the bar reaches the AI. It is deliberately *generous* -- its job is to
discard the obvious no, not to make the final call. Gemini makes the final call.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Iterable

from models import ARABIC_RANGE, JobPost, normalise_text

# Weights
W_PRIMARY = 3      # core VoIP/telephony skills
W_SECONDARY = 1    # adjacent IT-support skills
W_LOCATION = 1     # in a target country
DISQUALIFY = -100  # hard kill

# Seniority mismatches: these titles are not a fit for an early-career engineer
# and pollute the alert stream badly.
_TOO_SENIOR = re.compile(
    r"\b(chief|c[te]o|cio|vice president|\bvp\b|head of|director of|"
    r"director,|general manager|country manager|partner)\b",
    re.I,
)


_LATIN_CH = re.compile(r"[a-z0-9]", re.I)
_ARABIC_CH = re.compile(f"[{ARABIC_RANGE}]")


def _guard(char: str, *, lookbehind: bool) -> str:
    """Build a script-appropriate word boundary for one edge of a term.

    `\\b` is the obvious tool and the wrong one here. Python's `\\b` is defined
    against `\\w`, which includes Arabic, so `\\bدعم\\b` behaves inconsistently
    once the surrounding text mixes scripts, punctuation and RTL marks.

    Instead each edge asserts against the script of the adjacent character of
    the TERM itself:
      * a Latin edge must not touch another Latin letter/digit -- which is what
        stops `sip` matching inside `gossip`;
      * an Arabic edge must not touch another Arabic letter -- which is what
        stops `دعم` matching inside a longer word;
      * a punctuation/symbol edge gets no guard at all, because demanding one
        would break terms like `3cx` or `c++`.
    """
    if _LATIN_CH.fullmatch(char):
        body = "a-z0-9"
    elif _ARABIC_CH.fullmatch(char):
        body = ARABIC_RANGE
    else:
        return ""
    return f"(?<![{body}])" if lookbehind else f"(?![{body}])"


@lru_cache(maxsize=4096)
def _compile_terms(terms: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """Bilingual word-boundary patterns (English + Arabic).

    Terms are pushed through `normalise_text` FIRST, because that is what the
    haystack goes through in `score_text`. Skipping this silently breaks every
    Arabic term containing a folded letter -- `تقنية معلومات` (teh marbuta) would
    be compiled with ة while the text it is searching has already become ه, so
    it could never match its own subject. English terms are unaffected, which is
    exactly why the bug is invisible until someone tests the Arabic side.
    """
    out = []
    for raw in terms:
        term = normalise_text(raw)
        if not term:
            continue
        escaped = re.escape(term)
        # Multi-word terms tolerate flexible whitespace.
        escaped = escaped.replace(r"\ ", r"\s+")
        prefix = _guard(term[0], lookbehind=True)
        suffix = _guard(term[-1], lookbehind=False)
        out.append(re.compile(f"{prefix}{escaped}{suffix}", re.I | re.U))
    return tuple(out)


def _terms(profile: dict[str, Any], key: str) -> tuple[str, ...]:
    return tuple(str(t) for t in (profile.get(key) or []))


def _hits(text: str, patterns: Iterable[re.Pattern[str]]) -> list[str]:
    return [p.pattern for p in patterns if p.search(text)]


def score_text(text: str, profile: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Return (score, explanation). Negative score == disqualified."""
    norm = normalise_text(text)
    if not norm:
        return 0, {"reason": "empty"}

    negatives = _hits(norm, _compile_terms(_terms(profile, "negative_keywords")))
    if negatives:
        return DISQUALIFY, {"reason": "negative_keyword", "matched": negatives[:3]}

    primary = _hits(norm, _compile_terms(_terms(profile, "primary_keywords")))
    secondary = _hits(norm, _compile_terms(_terms(profile, "secondary_keywords")))
    locations = _hits(norm, _compile_terms(_terms(profile, "target_locations")))

    total = (
        W_PRIMARY * len(primary)
        + W_SECONDARY * len(secondary)
        + (W_LOCATION if locations else 0)
    )
    return total, {
        "primary": primary[:6],
        "secondary": secondary[:6],
        "locations": locations[:3],
    }


def score_job(job: JobPost, profile: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Score a posting. The title carries far more signal than the body, so it
    is weighted by counting it twice."""
    title = job.title or ""
    body = job.searchable_text()

    if title and _TOO_SENIOR.search(title):
        return DISQUALIFY, {"reason": "seniority_mismatch", "matched": [title[:60]]}

    title_score, title_why = score_text(title, profile)
    if title_score <= DISQUALIFY // 2:
        return DISQUALIFY, title_why

    body_score, body_why = score_text(body, profile)
    if body_score <= DISQUALIFY // 2:
        return DISQUALIFY, body_why

    total = body_score + title_score  # title counted twice, by design
    why = {
        "title_hits": title_why.get("primary", []) + title_why.get("secondary", []),
        "body_hits": body_why.get("primary", []),
        "locations": body_why.get("locations", []),
        "score": total,
    }
    return total, why


def prefilter(
    jobs: Iterable[JobPost], profile: dict[str, Any], minimum: int = 2
) -> tuple[list[JobPost], int, int]:
    """Split postings into (kept, dropped_irrelevant, dropped_disqualified).

    Kept jobs are returned sorted by descending score so that, if a per-run cap
    is hit, the best candidates are the ones that reach Gemini.
    """
    kept: list[JobPost] = []
    weak = 0
    disqualified = 0

    for job in jobs:
        value, _why = score_job(job, profile)
        if value <= DISQUALIFY // 2:
            disqualified += 1
            continue
        if value < minimum:
            weak += 1
            continue
        job.prefilter_score = value
        kept.append(job)

    kept.sort(key=lambda j: j.prefilter_score, reverse=True)
    return kept, weak, disqualified
