"""
Private store for credentials, application history and inbox events.

WHY THIS IS A SEPARATE DATABASE FROM db.py
------------------------------------------
`state/jobs.db` is force-pushed to the `bot-state` branch of a PUBLIC GitHub
repository on every scheduled run. That is exactly right for deduplication
state, and exactly wrong for anything in this file: platform passwords, the
Gmail app password, cover letters, salary expectations and recruiter emails.
Putting a `credentials_vault` table inside `jobs.db` would publish all of it.

So this store lives in `state/vault.db`, which is git-ignored, never copied by
the workflow, and encrypted at rest. The two databases are deliberately
unrelated files; nothing here can leak through the cloud sync.

ENCRYPTION
----------
Secrets are sealed with Fernet (AES-128-CBC + HMAC). The key comes from the
VAULT_KEY environment variable, or is generated once into `secrets/vault.key`
(also git-ignored). Losing the key means losing the stored passwords -- it does
not mean losing access to anything, since the accounts themselves still exist.

Encryption at rest matters less than the file simply never being published, but
it is cheap and it means a stray backup or a synced OneDrive folder does not
hand over every account.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from config import ROOT
from models import iso, utc_now

log = logging.getLogger(__name__)

VAULT_SCHEMA_VERSION = 3
DEFAULT_VAULT_PATH = ROOT / "state" / "vault.db"
KEY_PATH = ROOT / "secrets" / "vault.key"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Accounts on job boards. `password_encrypted` is Fernet ciphertext, never
-- plaintext, and this file is never synced anywhere.
CREATE TABLE IF NOT EXISTS credentials_vault (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_name      TEXT NOT NULL,
    platform_url       TEXT,
    email              TEXT,
    -- Boards are split on which identifier they sign you in by. Bayt and
    -- GulfTalent take the email; Wuzzuf and Tanqeeb show a separate handle you
    -- cannot recover from the email alone. Storing only the email meant half
    -- the vault could not actually log anyone in.
    username           TEXT,
    password_encrypted TEXT,
    profile_status     TEXT DEFAULT 'pending',
    notes              TEXT,
    created_at         TIMESTAMP NOT NULL,
    updated_at         TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vault_platform
    ON credentials_vault(platform_name, email);

-- One row per job we drafted or submitted an application for.
CREATE TABLE IF NOT EXISTS applications_history (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    job_fingerprint       TEXT UNIQUE NOT NULL,
    job_id                INTEGER,
    company               TEXT,
    role                  TEXT,
    platform              TEXT,
    job_url               TEXT,
    submitted_payload_json TEXT,
    cover_letter_text     TEXT,
    status                TEXT NOT NULL DEFAULT 'draft',
    screenshot_path       TEXT,
    failure_reason        TEXT,
    created_at            TIMESTAMP NOT NULL,
    submitted_at          TIMESTAMP,
    -- Written by the in-line edit engine: when the draft was last changed and
    -- how many times. `revision` is what tells the reader whether the card in
    -- front of them is the original or the third rewrite.
    updated_at            TIMESTAMP,
    revision              INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_apps_status ON applications_history(status);
CREATE INDEX IF NOT EXISTS idx_apps_jobid  ON applications_history(job_id);

-- Inbound recruiter mail, classified.
CREATE TABLE IF NOT EXISTS email_interview_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id     TEXT UNIQUE NOT NULL,
    sender         TEXT,
    subject        TEXT,
    classification TEXT,
    parsed_date    TEXT,
    meeting_link   TEXT,
    summary        TEXT,
    matched_app_id INTEGER,
    alerted        INTEGER NOT NULL DEFAULT 0,
    received_at    TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_class ON email_interview_events(classification);
"""

#: application lifecycle
STATUS_DRAFT = "draft"
STATUS_REVIEW = "review_pending"
STATUS_APPROVED = "approved"
STATUS_SUBMITTED = "submitted"
STATUS_FAILED = "failed"
STATUS_DECLINED = "declined"


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------
class VaultError(RuntimeError):
    pass


def _load_key() -> bytes:
    """Fetch the Fernet key from env, or create one on first use."""
    env_key = (os.environ.get("VAULT_KEY") or "").strip()
    if env_key:
        try:
            raw = env_key.encode()
            base64.urlsafe_b64decode(raw)  # validate shape
            return raw
        except Exception as exc:
            raise VaultError(f"VAULT_KEY is not a valid Fernet key: {exc}") from exc

    if KEY_PATH.exists():
        return KEY_PATH.read_text(encoding="utf-8").strip().encode()

    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise VaultError(
            "The `cryptography` package is required to store credentials. "
            "Run: pip install -r requirements.txt"
        ) from exc

    key = Fernet.generate_key()
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEY_PATH.write_text(key.decode(), encoding="utf-8")
    gitignore = KEY_PATH.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
    log.warning(
        "Generated a new vault key at %s. Back it up: without it the stored "
        "passwords cannot be read back.", KEY_PATH,
    )
    return key


def _fernet():
    from cryptography.fernet import Fernet

    return Fernet(_load_key())


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise VaultError(
            "Could not decrypt a stored password. The vault key changed or is "
            "missing; the account itself is unaffected -- reset the password on "
            "the platform and store it again."
        ) from exc


# ---------------------------------------------------------------------------
class SecureStore:
    """SQLite store for everything too sensitive to publish."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or os.environ.get("VAULT_PATH") or DEFAULT_VAULT_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                     timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._migrate()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    #: columns added after v1. The vault is a LOCAL file that is never
    #: recreated from scratch -- it holds the only copy of every stored
    #: password -- so new columns have to be added to the file already on disk.
    _ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
        "applications_history": [
            ("updated_at", "TIMESTAMP"),
            ("revision", "INTEGER NOT NULL DEFAULT 0"),
        ],
        "credentials_vault": [
            ("username", "TEXT"),
        ],
    }

    def _migrate(self) -> None:
        with self._tx() as c:
            c.executescript(_SCHEMA)
            for table, columns in self._ADDED_COLUMNS.items():
                existing = {
                    r["name"] for r in c.execute(f"PRAGMA table_info({table})")
                }
                for name, coltype in columns:
                    if name not in existing:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")
                        log.info("Migrated vault %s: added column %s.", table, name)
            c.execute(
                "INSERT INTO vault_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(VAULT_SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            except Exception:
                pass
            self._conn.close()

    # -- credentials --------------------------------------------------------
    def save_credentials(
        self, platform_name: str, platform_url: str, email: str,
        password: str, profile_status: str = "pending", notes: str = "",
        username: str = "",
    ) -> int:
        """Store (or update) an account. The password is encrypted here.

        `username` is preserved on conflict rather than overwritten with an
        empty string: a board hands you your real handle only after signup, so
        a later re-provision (which knows only the derived one) must not wipe
        the value that actually works.
        """
        now = iso(utc_now())
        with self._tx() as c:
            c.execute(
                "INSERT INTO credentials_vault "
                "(platform_name,platform_url,email,username,password_encrypted,"
                " profile_status,notes,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(platform_name,email) DO UPDATE SET "
                "  password_encrypted=excluded.password_encrypted,"
                "  platform_url=excluded.platform_url,"
                "  username=COALESCE(NULLIF(excluded.username,''), username),"
                "  profile_status=excluded.profile_status,"
                "  notes=excluded.notes, updated_at=excluded.updated_at",
                (platform_name, platform_url, email, username, encrypt(password),
                 profile_status, notes, now, now),
            )
            row = c.execute(
                "SELECT id FROM credentials_vault WHERE platform_name=? AND email=?",
                (platform_name, email),
            ).fetchone()
        return int(row["id"]) if row else 0

    def get_credentials(self, platform_name: str) -> dict[str, Any] | None:
        """Return one account with the password DECRYPTED, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM credentials_vault WHERE platform_name=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (platform_name,),
            ).fetchone()
        if not row:
            return None
        out = dict(row)
        out["password"] = decrypt(out.pop("password_encrypted", ""))
        return out

    def list_platforms(self) -> list[dict[str, Any]]:
        """Every stored account, WITHOUT decrypting anything."""
        with self._lock:
            return [
                {k: v for k, v in dict(r).items() if k != "password_encrypted"}
                for r in self._conn.execute(
                    "SELECT * FROM credentials_vault ORDER BY platform_name"
                )
            ]

    def set_profile_status(self, platform_name: str, status: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE credentials_vault SET profile_status=?, updated_at=? "
                "WHERE platform_name=?",
                (status, iso(utc_now()), platform_name),
            )

    # -- applications -------------------------------------------------------
    def record_application(
        self, *, job_fingerprint: str, job_id: int, company: str, role: str,
        platform: str, job_url: str = "", payload: dict[str, Any] | None = None,
        cover_letter: str = "", status: str = STATUS_DRAFT,
    ) -> int:
        now = iso(utc_now())
        with self._tx() as c:
            c.execute(
                "INSERT INTO applications_history "
                "(job_fingerprint,job_id,company,role,platform,job_url,"
                " submitted_payload_json,cover_letter_text,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(job_fingerprint) DO UPDATE SET "
                "  submitted_payload_json=excluded.submitted_payload_json,"
                "  cover_letter_text=excluded.cover_letter_text,"
                "  status=excluded.status",
                (job_fingerprint, job_id, company, role, platform, job_url,
                 json.dumps(payload or {}, ensure_ascii=False),
                 cover_letter, status, now),
            )
            row = c.execute(
                "SELECT id FROM applications_history WHERE job_fingerprint=?",
                (job_fingerprint,),
            ).fetchone()
        return int(row["id"]) if row else 0

    def set_application_status(
        self, app_id: int, status: str, *, screenshot_path: str = "",
        failure_reason: str = "",
    ) -> None:
        """Move an application along its lifecycle.

        The two non-status columns are treated differently on purpose:

          * `screenshot_path` is EVIDENCE that a submission happened, so it is
            only ever overwritten by a new path -- never cleared. This used to
            be a plain assignment, which meant re-running `--approve` on an
            already-submitted application silently threw away the proof.
          * `failure_reason` IS cleared when none is supplied, because a stale
            reason shown against a since-approved application is worse than no
            reason at all.
        """
        with self._tx() as c:
            c.execute(
                "UPDATE applications_history SET status=?, "
                "screenshot_path=COALESCE(NULLIF(?,''), screenshot_path), "
                "failure_reason=NULLIF(?,''), "
                "submitted_at=CASE WHEN ?='submitted' "
                "THEN ? ELSE submitted_at END WHERE id=?",
                (status, screenshot_path or "", failure_reason or "",
                 status, iso(utc_now()), app_id),
            )

    def get_application(self, app_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM applications_history WHERE id=?", (app_id,)
            ).fetchone()
        return dict(row) if row else None

    def application_for_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM applications_history WHERE job_fingerprint=?",
                (fingerprint,),
            ).fetchone()
        return dict(row) if row else None

    def update_application_draft(
        self, app_id: int, *, payload: dict[str, Any] | None = None,
        cover_letter: str | None = None, status: str | None = None,
    ) -> bool:
        """Rewrite a draft in place after an in-line edit. Returns False if unknown.

        Both columns move together on purpose. `cover_letter_text` is what the
        review card shows; `submitted_payload_json["fields"]` is what actually
        gets typed into the form. An edit that touched only the first would look
        applied on the card and submit the ORIGINAL text -- a silent, invisible
        failure of exactly the kind the human gate exists to prevent. Callers
        build both halves with `auto_apply.review.apply_edit`, which keeps them
        consistent.

        `revision` is bumped on every call so a card can say which version of
        the draft the reader is looking at, and `updated_at` records when.
        A None argument leaves that column untouched.
        """
        if not self.get_application(app_id):
            return False
        with self._tx() as c:
            c.execute(
                "UPDATE applications_history SET "
                "  submitted_payload_json=COALESCE(?, submitted_payload_json),"
                "  cover_letter_text=COALESCE(?, cover_letter_text),"
                "  status=COALESCE(?, status),"
                "  revision=COALESCE(revision,0)+1,"
                "  updated_at=? "
                "WHERE id=?",
                (
                    json.dumps(payload, ensure_ascii=False)
                    if payload is not None else None,
                    cover_letter,
                    status,
                    iso(utc_now()),
                    app_id,
                ),
            )
        return True

    #: the order the HITL listener resolves a bare "done" against. A draft
    #: awaiting review is the obvious target; an already-approved one that was
    #: never submitted is the next most likely thing the user means.
    PENDING_ORDER = (STATUS_REVIEW, STATUS_APPROVED, STATUS_DRAFT)

    def latest_pending_application(self) -> dict[str, Any] | None:
        """The draft a bare `done` / `موافق` should act on.

        Newest-first within each status band rather than newest overall: a
        `review_pending` draft created this morning outranks an `approved` one
        from ten minutes ago, because approving something twice is harmless and
        submitting the wrong job is not.
        """
        for status in self.PENDING_ORDER:
            rows = self.applications_by_status(status)
            if rows:
                return rows[0]
        return None

    def applications_awaiting_review(self, limit: int = 20) -> list[dict[str, Any]]:
        """Everything still waiting on a human, newest first."""
        placeholders = ",".join("?" * len(self.PENDING_ORDER))
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                f"SELECT * FROM applications_history WHERE status IN "
                f"({placeholders}) ORDER BY id DESC LIMIT ?",
                (*self.PENDING_ORDER, int(limit)),
            )]

    def applications_by_status(self, status: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM applications_history WHERE status=? "
                "ORDER BY id DESC", (status,)
            )]

    #: words that carry no identifying weight in a company name
    _COMPANY_NOISE = {
        "group", "holding", "holdings", "company", "co", "corp", "corporation",
        "inc", "llc", "ltd", "limited", "plc", "sa", "sarl", "gmbh", "bv",
        "technologies", "technology", "tech", "solutions", "systems", "services",
        "international", "global", "middle", "east", "consulting", "consultancy",
        "the", "and", "for", "of", "egypt", "uae", "ksa", "qatar", "careers", "hr",
    }

    def find_application_by_company(self, company: str) -> dict[str, Any] | None:
        """Tie an inbound email back to an application, tolerantly.

        A recruiter writes from "Etisalat Group Careers" about an application
        stored as "Etisalat". A substring match in either direction misses that,
        so this compares SIGNIFICANT TOKENS instead: strip the corporate filler
        ("group", "llc", "solutions") and look for a shared distinctive word.

        Matching is deliberately generous. Attaching an interview email to the
        wrong application is a small annoyance; failing to attach it at all
        means the alert arrives without the context that makes it useful.
        """
        tokens = {
            t for t in re.split(r"[^a-z0-9؀-ۿ]+", (company or "").lower())
            if len(t) > 2 and t not in self._COMPANY_NOISE
        }
        if not tokens:
            return None

        with self._lock:
            rows = [dict(r) for r in self._conn.execute(
                "SELECT * FROM applications_history "
                "WHERE company IS NOT NULL AND company != '' "
                "ORDER BY id DESC LIMIT 300"
            )]

        best, best_score = None, 0
        for row in rows:
            stored = {
                t for t in re.split(
                    r"[^a-z0-9؀-ۿ]+", str(row["company"]).lower()
                )
                if len(t) > 2 and t not in self._COMPANY_NOISE
            }
            score = len(tokens & stored)
            if score > best_score:
                best, best_score = row, score
        return best

    # -- inbox events -------------------------------------------------------
    def seen_message(self, message_id: str) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM email_interview_events WHERE message_id=? LIMIT 1",
                (message_id,),
            ).fetchone() is not None

    def record_email_event(
        self, *, message_id: str, sender: str, subject: str,
        classification: str, parsed_date: str = "", meeting_link: str = "",
        summary: str = "", matched_app_id: int | None = None,
        alerted: bool = False,
    ) -> int:
        """Bank one triaged message. Idempotent on `message_id`.

        This row IS the anti-duplicate key: `message_id` is UNIQUE and
        `seen_message()` reads exactly this table, so a message that has a row
        can never be classified, alerted or charged to the Gemini quota twice
        -- however many times the poll laps the same inbox.

        Returns the row id, re-read on conflict. `lastrowid` is meaningless
        after `DO NOTHING`: SQLite leaves it pointing at whatever this cursor
        last inserted, so the previous caller's id would be handed back as if
        it were this message's.
        """
        with self._tx() as c:
            c.execute(
                "INSERT INTO email_interview_events "
                "(message_id,sender,subject,classification,parsed_date,"
                " meeting_link,summary,matched_app_id,alerted,received_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(message_id) DO NOTHING",
                (message_id, sender, subject, classification, parsed_date,
                 meeting_link, summary, matched_app_id, int(alerted),
                 iso(utc_now())),
            )
            row = c.execute(
                "SELECT id FROM email_interview_events WHERE message_id=?",
                (message_id,),
            ).fetchone()
        return int(row["id"]) if row else 0

    def mark_event_alerted(self, message_id: str, alerted: bool = True) -> None:
        """Record whether the alert for this message actually went out.

        Split from `record_email_event` on purpose. The row is written BEFORE
        the send so a crash mid-delivery cannot produce a second alert; this
        then records what really happened, so `alerted` means "the user saw
        it" rather than "we intended to tell them".
        """
        with self._tx() as c:
            c.execute(
                "UPDATE email_interview_events SET alerted=? WHERE message_id=?",
                (int(alerted), message_id),
            )

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM email_interview_events "
                "ORDER BY id DESC LIMIT ?", (limit,)
            )]

    # -- reporting ----------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        with self._lock:
            def one(q: str) -> int:
                row = self._conn.execute(q).fetchone()
                return int(row[0]) if row else 0

            by_status = {
                r["status"]: r["n"] for r in self._conn.execute(
                    "SELECT status, COUNT(*) AS n FROM applications_history "
                    "GROUP BY status"
                )
            }
            by_class = {
                r["classification"]: r["n"] for r in self._conn.execute(
                    "SELECT classification, COUNT(*) AS n "
                    "FROM email_interview_events GROUP BY classification"
                )
            }
            return {
                "platforms": one("SELECT COUNT(*) FROM credentials_vault"),
                "applications": one("SELECT COUNT(*) FROM applications_history"),
                "applications_by_status": by_status,
                "email_events": one("SELECT COUNT(*) FROM email_interview_events"),
                "events_by_class": by_class,
                "vault_bytes": self.path.stat().st_size if self.path.exists() else 0,
            }
