"""
Persistent deduplication + audit layer (SQLite).

This is the component that guarantees the user is never messaged about the same
job twice -- across process restarts, across cloud runners, across months. In
the GitHub Actions deployment this single file is checked out from, and pushed
back to, the orphan `bot-state` branch on every run, so state survives even
though the runner itself is destroyed after each execution.

Design notes:
  * journal_mode=DELETE (not WAL) so the database is always exactly ONE file --
    critical, because the cloud deployment version-controls that file.
  * Two independent dedup keys: content fingerprint AND canonical URL hash. A
    posting syndicated to three boards collapses to one alert; a board that
    rewrites its own titles still collapses on URL.
  * Every state transition is recorded, so `python main.py --stats` can explain
    exactly why any given job did or did not produce an alert.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from models import Evaluation, JobPost, iso, utc_now

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_jobs (
    fingerprint   TEXT PRIMARY KEY,
    url_hash      TEXT,
    url           TEXT,
    source        TEXT,
    title         TEXT,
    company       TEXT,
    location      TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    times_seen    INTEGER NOT NULL DEFAULT 1,
    evaluated     INTEGER NOT NULL DEFAULT 0,
    match_score   INTEGER,
    notified      INTEGER NOT NULL DEFAULT 0,
    notified_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_seen_url_hash  ON seen_jobs(url_hash);
CREATE INDEX IF NOT EXISTS idx_seen_notified  ON seen_jobs(notified);
CREATE INDEX IF NOT EXISTS idx_seen_last_seen ON seen_jobs(last_seen);

CREATE TABLE IF NOT EXISTS evaluations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT NOT NULL,
    match_score  INTEGER NOT NULL,
    company_name TEXT,
    role_title   TEXT,
    location     TEXT,
    why_matched  TEXT,
    skill_gaps   TEXT,
    model        TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_fp ON evaluations(fingerprint);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    channel     TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT,
    sent_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_fp ON alerts(fingerprint);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    scraped      INTEGER DEFAULT 0,
    fresh        INTEGER DEFAULT 0,
    evaluated    INTEGER DEFAULT 0,
    matched      INTEGER DEFAULT 0,
    alerted      INTEGER DEFAULT 0,
    status       TEXT,
    detail       TEXT
);
"""


class Database:
    """Thread-safe SQLite wrapper. One instance per process."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        # DELETE journalling keeps the DB to a single portable file, which the
        # GitHub Actions deployment commits to the bot-state branch verbatim.
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # -- plumbing -----------------------------------------------------------
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _migrate(self) -> None:
        with self._tx() as c:
            c.executescript(_SCHEMA)
            c.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.execute("PRAGMA optimize")
            except Exception:
                pass
            self._conn.close()

    # -- key/value ----------------------------------------------------------
    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    # -- deduplication ------------------------------------------------------
    def known_fingerprints(self, fps: Sequence[str]) -> set[str]:
        if not fps:
            return set()
        out: set[str] = set()
        with self._lock:
            for i in range(0, len(fps), 400):  # stay under SQLite's var limit
                chunk = fps[i : i + 400]
                q = f"SELECT fingerprint FROM seen_jobs WHERE fingerprint IN ({','.join('?' * len(chunk))})"
                out.update(r["fingerprint"] for r in self._conn.execute(q, chunk))
        return out

    def known_url_hashes(self, hashes: Sequence[str]) -> set[str]:
        hashes = [h for h in hashes if h]
        if not hashes:
            return set()
        out: set[str] = set()
        with self._lock:
            for i in range(0, len(hashes), 400):
                chunk = hashes[i : i + 400]
                q = f"SELECT url_hash FROM seen_jobs WHERE url_hash IN ({','.join('?' * len(chunk))})"
                out.update(r["url_hash"] for r in self._conn.execute(q, chunk) if r["url_hash"])
        return out

    def partition_new(self, jobs: Iterable[JobPost]) -> tuple[list[JobPost], int]:
        """Split a scrape batch into (never-seen-before, duplicate-count).

        Also collapses duplicates *within* the batch itself, which is common:
        the same role legitimately appears on LinkedIn and in a Telegram channel
        in the same run.
        """
        jobs = list(jobs)
        if not jobs:
            return [], 0

        batch_fps: set[str] = set()
        batch_urls: set[str] = set()
        deduped: list[JobPost] = []
        intra = 0
        for job in jobs:
            fp, uh = job.fingerprint, job.url_hash
            if fp in batch_fps or (uh and uh in batch_urls):
                intra += 1
                continue
            batch_fps.add(fp)
            if uh:
                batch_urls.add(uh)
            deduped.append(job)

        known_fp = self.known_fingerprints([j.fingerprint for j in deduped])
        known_uh = self.known_url_hashes([j.url_hash for j in deduped])

        fresh: list[JobPost] = []
        repeats: list[JobPost] = []
        for job in deduped:
            if job.fingerprint in known_fp or (job.url_hash and job.url_hash in known_uh):
                repeats.append(job)
            else:
                fresh.append(job)

        if repeats:
            self.touch_seen(repeats)
        return fresh, intra + len(repeats)

    def touch_seen(self, jobs: Iterable[JobPost]) -> None:
        """Bump last_seen/times_seen for postings we already knew about."""
        rows = [(iso(utc_now()), j.fingerprint) for j in jobs]
        if not rows:
            return
        with self._tx() as c:
            c.executemany(
                "UPDATE seen_jobs SET last_seen=?, times_seen=times_seen+1 "
                "WHERE fingerprint=?",
                rows,
            )

    def record_seen(self, jobs: Iterable[JobPost]) -> int:
        """Insert postings as seen. Idempotent."""
        now = iso(utc_now())
        rows = [
            (
                j.fingerprint, j.url_hash, j.url, j.source,
                j.title, j.company, j.location, now, now,
            )
            for j in jobs
        ]
        if not rows:
            return 0
        with self._tx() as c:
            c.executemany(
                "INSERT INTO seen_jobs "
                "(fingerprint,url_hash,url,source,title,company,location,"
                " first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(fingerprint) DO UPDATE SET "
                "  last_seen=excluded.last_seen, times_seen=times_seen+1",
                rows,
            )
        return len(rows)

    # -- evaluations --------------------------------------------------------
    def record_evaluation(self, ev: Evaluation) -> None:
        now = iso(utc_now())
        with self._tx() as c:
            c.execute(
                "INSERT INTO evaluations "
                "(fingerprint,match_score,company_name,role_title,location,"
                " why_matched,skill_gaps,model,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ev.fingerprint, ev.match_score, ev.company_name, ev.role_title,
                    ev.location, ev.why_matched,
                    json.dumps(ev.skill_gaps, ensure_ascii=False), ev.model, now,
                ),
            )
            c.execute(
                "UPDATE seen_jobs SET evaluated=1, match_score=? WHERE fingerprint=?",
                (ev.match_score, ev.fingerprint),
            )

    # -- alerts -------------------------------------------------------------
    def already_alerted(self, fingerprint: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM alerts WHERE fingerprint=? AND status='sent' LIMIT 1",
                (fingerprint,),
            ).fetchone()
        return row is not None

    def record_alert(
        self, fingerprint: str, channel: str, status: str, detail: str = ""
    ) -> None:
        now = iso(utc_now())
        with self._tx() as c:
            c.execute(
                "INSERT INTO alerts(fingerprint,channel,status,detail,sent_at) "
                "VALUES (?,?,?,?,?)",
                (fingerprint, channel, status, detail[:500], now),
            )
            if status == "sent":
                c.execute(
                    "UPDATE seen_jobs SET notified=1, notified_at=? WHERE fingerprint=?",
                    (now, fingerprint),
                )

    # -- cooldowns (failure alerts, heartbeats) -----------------------------
    def cooldown_active(self, key: str, minutes: int) -> bool:
        raw = self.get_meta(f"cooldown:{key}")
        if not raw:
            return False
        try:
            last = datetime.fromisoformat(raw)
        except ValueError:
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return utc_now() - last < timedelta(minutes=minutes)

    def mark_cooldown(self, key: str) -> None:
        self.set_meta(f"cooldown:{key}", iso(utc_now()))

    # -- run bookkeeping ----------------------------------------------------
    def start_run(self) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO runs(started_at,status) VALUES(?,'running')",
                (iso(utc_now()),),
            )
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, *, status: str, detail: str = "", **counts: int) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, detail=?, scraped=?, "
                "fresh=?, evaluated=?, matched=?, alerted=? WHERE id=?",
                (
                    iso(utc_now()), status, detail[:1000],
                    counts.get("scraped", 0), counts.get("fresh", 0),
                    counts.get("evaluated", 0), counts.get("matched", 0),
                    counts.get("alerted", 0), run_id,
                ),
            )

    # -- housekeeping -------------------------------------------------------
    def prune(self, keep_days: int = 180) -> int:
        """Drop ancient never-notified records so the committed DB stays small."""
        cutoff = iso(utc_now() - timedelta(days=keep_days))
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM seen_jobs WHERE last_seen < ? AND notified=0", (cutoff,)
            )
            removed = cur.rowcount or 0
            c.execute("DELETE FROM evaluations WHERE created_at < ?", (cutoff,))
            c.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
        if removed:
            with self._lock:
                self._conn.execute("VACUUM")
            log.info("Pruned %d stale records from the dedup store.", removed)
        return removed

    # -- reporting ----------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        with self._lock:
            c = self._conn
            def one(q: str, *a: Any) -> Any:
                row = c.execute(q, a).fetchone()
                return row[0] if row else 0

            recent = [
                dict(r) for r in c.execute(
                    "SELECT started_at,status,scraped,fresh,evaluated,matched,"
                    "alerted FROM runs ORDER BY id DESC LIMIT 10"
                )
            ]
            top = [
                dict(r) for r in c.execute(
                    "SELECT role_title,company_name,match_score,location,created_at "
                    "FROM evaluations ORDER BY match_score DESC, id DESC LIMIT 10"
                )
            ]
            by_source = [
                dict(r) for r in c.execute(
                    "SELECT source, COUNT(*) AS n FROM seen_jobs "
                    "GROUP BY source ORDER BY n DESC LIMIT 20"
                )
            ]
            return {
                "total_jobs_seen": one("SELECT COUNT(*) FROM seen_jobs"),
                "total_evaluated": one("SELECT COUNT(*) FROM seen_jobs WHERE evaluated=1"),
                "total_alerts_sent": one("SELECT COUNT(*) FROM alerts WHERE status='sent'"),
                "total_runs": one("SELECT COUNT(*) FROM runs"),
                "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
                "recent_runs": recent,
                "top_matches": top,
                "jobs_by_source": by_source,
            }
