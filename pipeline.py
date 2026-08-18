"""
The pipeline: raw web -> WhatsApp alert.

    ingest -> age-gate -> deduplicate -> lexical pre-filter -> Gemini ->
    threshold -> dispatch -> persist -> report

Each stage narrows the funnel and is counted, so `run_report.json` explains
exactly where every posting was lost. That report is what makes a scheduled job
you never watch actually debuggable.

The order is deliberate: deduplication happens BEFORE the pre-filter and the
pre-filter BEFORE Gemini, so the expensive stage only ever sees postings that
are both new and plausible.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import scrapers
from config import ROOT, settings
from cv_profile import CVError, load_cv
from db import Database
from evaluator import GeminiEvaluator
from models import Evaluation, JobPost, iso, utc_now
from notifier import WhatsAppNotifier
from relevance import prefilter

log = logging.getLogger(__name__)

REPORT_PATH = ROOT / "run_report.json"


@dataclass
class RunReport:
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    status: str = "ok"

    scraped: int = 0
    too_old: int = 0
    duplicates: int = 0
    prefilter_dropped: int = 0
    prefilter_disqualified: int = 0
    over_cap: int = 0
    evaluated: int = 0
    matched: int = 0
    alerts_sent: int = 0
    alerts_failed: int = 0
    alerts_skipped: int = 0

    gemini_calls: int = 0
    gemini_tokens: int = 0
    sources: list[dict[str, Any]] = field(default_factory=list)
    top_matches: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def headline(self) -> str:
        return (
            f"scraped {self.scraped} -> fresh {self.scraped - self.duplicates - self.too_old} "
            f"-> evaluated {self.evaluated} -> matched {self.matched} "
            f"-> alerted {self.alerts_sent}"
        )


def _age_gate(jobs: list[JobPost], max_days: int) -> tuple[list[JobPost], int]:
    """Drop stale postings. Unknown timestamps are KEPT -- many good sources
    (Telegram, some RSS) simply do not publish one, and discarding them would
    silently remove a whole source."""
    if max_days <= 0:
        return jobs, 0
    kept, dropped = [], 0
    for job in jobs:
        age = job.age_days
        if age is not None and age > max_days:
            dropped += 1
            continue
        kept.append(job)
    return kept, dropped


def apply_eval_cap(
    candidates: list[JobPost], cap: int
) -> tuple[list[JobPost], list[JobPost]]:
    """Split candidates into (evaluate_now, defer_to_next_run).

    Candidates arrive sorted best-first, so the cap always keeps the strongest.
    Deferred postings are returned separately because the caller must NOT mark
    them as seen -- see the note at the call site.
    """
    if cap <= 0 or len(candidates) <= cap:
        return candidates, []
    return candidates[:cap], candidates[cap:]


def handle_live_job(
    job: JobPost,
    db: Database,
    notifier: WhatsAppNotifier,
    cv_text: str,
    evaluator: GeminiEvaluator | None = None,
) -> bool:
    """Single-posting fast path for the real-time Telegram listener.

    Same stages as a batch run -- dedupe, pre-filter, score, threshold, alert --
    but for one message, so an alert can land on WhatsApp seconds after the post
    appears in the group rather than at the next scheduled sweep.

    Returns True if an alert was sent. Never raises: this runs inside an event
    handler, and an exception here would take the listener down.
    """
    try:
        fresh, _dupes = db.partition_new([job])
        if not fresh:
            log.debug("LIVE: already seen -- %s", job.title[:60])
            return False

        candidates, _weak, _dq = prefilter(
            fresh, settings.profile,
            int(settings.engine.get("prefilter_min_score", 2)),
        )
        db.record_seen(fresh)
        if not candidates:
            log.info("LIVE: pre-filter dropped %r", job.title[:60])
            return False

        evaluator = evaluator or GeminiEvaluator()
        evaluations = evaluator.evaluate(candidates, cv_text)
        for ev in evaluations:
            db.record_evaluation(ev)

        threshold = settings.match_threshold
        matches = [
            e for e in evaluations if e.match_score >= threshold and not e.error
        ]
        if not matches:
            best = max((e.match_score for e in evaluations), default=0)
            log.info("LIVE: scored %d%%, below the %d%% bar -- %s",
                     best, threshold, job.title[:60])
            return False

        log.info("LIVE MATCH %d%% -- %s", matches[0].match_score, job.title[:70])
        result = notifier.dispatch(matches)
        return result.sent > 0
    except Exception as exc:
        log.exception("LIVE handler failed on %r: %s", job.title[:60], exc)
        return False


def run_once(db: Database, notifier: WhatsAppNotifier | None = None) -> RunReport:
    """Execute one complete hunt. Never raises -- failures land in the report."""
    started = time.monotonic()
    report = RunReport(started_at=iso(utc_now()))
    run_id = db.start_run()
    notifier = notifier or WhatsAppNotifier(db)
    engine = settings.engine

    # -- 0. the CV is a hard prerequisite ----------------------------------
    try:
        cv = load_cv()
    except CVError as exc:
        report.status = "fatal"
        report.errors.append(str(exc))
        log.error("%s", exc)
        db.finish_run(run_id, status="fatal", detail=str(exc)[:500])
        _write_report(report)
        if settings.notifications.get("send_failure_alerts", True):
            notifier.send_failure_alert("The master CV could not be loaded.")
        return report

    # -- 1. ingest ----------------------------------------------------------
    built = scrapers.build_scrapers(settings, db=db)
    raw, results = scrapers.run_all(
        built, max_workers=int(engine.get("scraper_concurrency", 8))
    )
    report.scraped = len(raw)
    report.sources = [
        {"name": r.name, "ok": r.ok, "count": r.count,
         "seconds": round(r.duration_s, 1), "error": r.error}
        for r in sorted(results, key=lambda r: -r.count)
    ]
    report.errors.extend(f"{r.name}: {r.error}" for r in results if not r.ok)

    healthy = [r for r in results if r.ok and r.count > 0]
    if not healthy:
        report.status = "degraded"
        msg = "Every ingestion source returned zero postings."
        log.error("%s", msg)
        report.errors.append(msg)
        if settings.notifications.get("send_failure_alerts", True):
            notifier.send_failure_alert(
                msg + " The bot cannot find jobs until at least one source recovers."
            )

    # -- 2. age gate --------------------------------------------------------
    fresh_enough, report.too_old = _age_gate(
        raw, int(engine.get("max_job_age_days", 21))
    )

    # -- 3. deduplicate against everything ever seen ------------------------
    new_jobs, report.duplicates = db.partition_new(fresh_enough)
    log.info(
        "Funnel: %d scraped -> %d recent -> %d never-seen-before",
        report.scraped, len(fresh_enough), len(new_jobs),
    )

    # -- 4. lexical pre-filter (protects the Gemini quota) ------------------
    candidates, report.prefilter_dropped, report.prefilter_disqualified = prefilter(
        new_jobs, settings.profile, int(engine.get("prefilter_min_score", 2))
    )
    log.info(
        "Pre-filter: %d candidates (%d weak, %d disqualified)",
        len(candidates), report.prefilter_dropped, report.prefilter_disqualified,
    )

    candidates, deferred = apply_eval_cap(
        candidates, int(engine.get("max_evaluations_per_run", 120))
    )
    report.over_cap = len(deferred)
    if deferred:
        log.info(
            "Capping evaluation at %d candidates; %d deferred to the next run.",
            len(candidates), report.over_cap,
        )

    # Mark as seen EVERYTHING except the deferred candidates.
    #
    # Recording before evaluation is what makes a mid-run crash safe: those
    # postings are already banked, so the next run cannot re-alert on them.
    # But a deferred candidate must stay UNSEEN -- marking it would retire a
    # job that was never actually looked at, and on a first run (with a large
    # backlog) that silently discards the tail of the queue forever.
    deferred_fps = {j.fingerprint for j in deferred}
    db.record_seen([j for j in new_jobs if j.fingerprint not in deferred_fps])
    if deferred:
        log.info(
            "%d deferred posting(s) left unrecorded so the next run re-queues them.",
            len(deferred),
        )

    # -- 5. Gemini ----------------------------------------------------------
    evaluations: list[Evaluation] = []
    if candidates:
        evaluator = GeminiEvaluator()

        def persist(batch: list[Evaluation]) -> None:
            """Write each batch the moment it lands.

            Without this, a timeout on batch 14 of 15 would throw away every
            verdict already paid for -- and the next run would re-evaluate the
            same postings, burning the quota twice.
            """
            for ev in batch:
                db.record_evaluation(ev)

        evaluations = evaluator.evaluate(candidates, cv.to_prompt(), on_batch=persist)
        report.gemini_calls = evaluator.calls_made
        report.gemini_tokens = evaluator.tokens_used
        report.evaluated = len(evaluations)
        failed = [e for e in evaluations if e.error and e.error != "not_a_real_vacancy"]
        if failed and len(failed) == len(evaluations):
            report.status = "degraded"
            report.errors.append(f"Gemini failed on every batch: {failed[0].error}")

    # -- 6. threshold + dispatch -------------------------------------------
    threshold = settings.match_threshold
    matches = sorted(
        (e for e in evaluations if e.match_score >= threshold and not e.error),
        key=lambda e: e.match_score,
        reverse=True,
    )
    report.matched = len(matches)

    max_alerts = int(engine.get("max_alerts_per_run", 12))
    if len(matches) > max_alerts:
        log.warning(
            "%d matches exceeded the per-run alert cap of %d; sending the best %d.",
            len(matches), max_alerts, max_alerts,
        )
        matches = matches[:max_alerts]

    if matches:
        dispatch = notifier.dispatch(matches)
        report.alerts_sent = dispatch.sent
        report.alerts_failed = dispatch.failed
        report.alerts_skipped = dispatch.skipped
        report.errors.extend(dispatch.errors[:5])
    else:
        log.info("No posting cleared the %d%% threshold this run.", threshold)

    report.top_matches = [
        {
            "score": e.match_score, "role": e.role_title, "company": e.company_name,
            "location": e.location, "source": e.source_platform, "link": e.direct_link,
        }
        for e in sorted(evaluations, key=lambda e: e.match_score, reverse=True)[:10]
    ]

    # -- 7. wrap up ---------------------------------------------------------
    report.duration_s = round(time.monotonic() - started, 1)
    report.finished_at = iso(utc_now())
    db.finish_run(
        run_id,
        status=report.status,
        detail=report.headline(),
        scraped=report.scraped,
        fresh=len(new_jobs),
        evaluated=report.evaluated,
        matched=report.matched,
        alerted=report.alerts_sent,
    )
    _write_report(report)

    if (
        settings.notifications.get("send_heartbeat", False)
        and report.alerts_sent == 0
    ):
        notifier.send_heartbeat(report.headline())

    log.info("RUN COMPLETE in %.1fs -- %s", report.duration_s, report.headline())
    return report


def _write_report(report: RunReport, path: Path = REPORT_PATH) -> None:
    try:
        path.write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        log.debug("Could not write %s: %s", path, exc)
