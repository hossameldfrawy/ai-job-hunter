"""
Both databases: the dedup keys, the migrations, the encryption, and what
happens when a write fails half way through.

WHY TWO DATABASES, AND WHY THAT IS A CORRECTNESS PROPERTY
---------------------------------------------------------
`state/jobs.db` is force-pushed to an orphan branch of a PUBLIC repository on
every scheduled run. `state/vault.db` holds platform passwords, the Gmail
credential, cover letters and salary expectations. They are separate files
because putting a `credentials_vault` table inside `jobs.db` would publish all
of it -- so "these are different files, and only one of them is synced" is
asserted here rather than assumed.

THE FOUR DEDUP KEYS
-------------------
Every one of them is the only thing standing between the user and a duplicate:

  fingerprint      content identity -- the same role on three boards is ONE alert
  url_hash         a board that rewrites its own titles still collapses
  ref_id           the short handle (#101) the WhatsApp card points at; it must
                   be STABLE, because it is the only route back to the job
  message_id       one inbound email is classified, billed and alerted once

Run:  python -m pytest tests/test_db_integrity.py -v
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as db_mod                                              # noqa: E402
import vault as vault_mod                                        # noqa: E402
from db import SCHEMA_VERSION, Database                          # noqa: E402
from models import Evaluation, JobPost, iso, utc_now             # noqa: E402
from vault import (                                              # noqa: E402
    STATUS_APPROVED, STATUS_DECLINED, STATUS_FAILED, STATUS_REVIEW,
    STATUS_SUBMITTED, VAULT_SCHEMA_VERSION, SecureStore, VaultError,
    decrypt, encrypt,
)


def _tmp(name: str) -> Path:
    return Path(tempfile.mkdtemp()) / name


def _job(title="VoIP Engineer", company="Etisalat", location="Dubai, UAE",
         url="https://uae.tanqeeb.com/jobs/1.html", source="tanqeeb:uae"):
    return JobPost(source=source, title=title, company=company,
                   location=location, url=url,
                   description="Asterisk and SIP trunks.")


# ---------------------------------------------------------------------------
class DatabaseHarness(unittest.TestCase):
    def setUp(self):
        self.path = _tmp("jobs.db")
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()


class TestFingerprintDedup(DatabaseHarness):
    def test_the_same_posting_is_new_exactly_once(self):
        fresh, dupes = self.db.partition_new([_job()])
        self.assertEqual((len(fresh), dupes), (1, 0))
        self.db.record_seen(fresh)
        fresh, dupes = self.db.partition_new([_job()])
        self.assertEqual((len(fresh), dupes), (0, 1))

    def test_the_same_role_syndicated_to_three_boards_is_one_alert(self):
        """Content identity, not URL identity -- that is the whole point."""
        batch = [
            _job(source="linkedin", url="https://linkedin.com/jobs/1"),
            _job(source="tanqeeb:uae", url="https://uae.tanqeeb.com/2"),
            _job(source="talent:ae", url="https://ae.talent.com/3"),
        ]
        fresh, dupes = self.db.partition_new(batch)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(dupes, 2)

    def test_a_board_that_rewrites_its_titles_still_collapses_on_url(self):
        url = "https://uae.tanqeeb.com/jobs/1.html"
        self.db.record_seen([_job(title="VoIP Engineer", url=url)])
        fresh, dupes = self.db.partition_new(
            [_job(title="URGENT!! VoIP Engineer needed now", company="Different",
                  url=url)]
        )
        self.assertEqual((len(fresh), dupes), (0, 1))

    def test_tracking_parameters_do_not_defeat_url_dedup(self):
        base = "https://uae.tanqeeb.com/jobs/1.html"
        self.db.record_seen([_job(url=base)])
        fresh, _ = self.db.partition_new(
            [_job(company="Other", title="Other role",
                  url=base + "?utm_source=x&trk=y&refId=z")]
        )
        self.assertEqual(len(fresh), 0)

    def test_state_survives_a_process_restart(self):
        self.db.record_seen([_job()])
        self.db.close()
        reopened = Database(self.path)
        try:
            fresh, _ = reopened.partition_new([_job()])
            self.assertEqual(fresh, [])
        finally:
            reopened.close()

    def test_intra_batch_duplicates_collapse_before_the_database_is_touched(self):
        fresh, dupes = self.db.partition_new([_job(), _job(), _job()])
        self.assertEqual((len(fresh), dupes), (1, 2))

    def test_a_repeat_sighting_bumps_the_counters(self):
        self.db.record_seen([_job()])
        self.db.partition_new([_job()])
        row = self.db._conn.execute(
            "SELECT times_seen FROM seen_jobs"
        ).fetchone()
        self.assertGreaterEqual(row["times_seen"], 2)

    def test_an_empty_batch_is_a_clean_no_op(self):
        self.assertEqual(self.db.partition_new([]), ([], 0))
        self.assertEqual(self.db.record_seen([]), 0)
        self.assertEqual(self.db.known_fingerprints([]), set())
        self.assertEqual(self.db.known_url_hashes([]), set())

    def test_lookups_chunk_past_sqlites_variable_limit(self):
        """999 bound variables is a hard SQLite limit; a 5,000-job run is normal."""
        jobs = [_job(title=f"Engineer {i}", url=f"https://x.com/{i}")
                for i in range(1500)]
        self.db.record_seen(jobs)
        known = self.db.known_fingerprints([j.fingerprint for j in jobs])
        self.assertEqual(len(known), 1500)


class TestReferenceIds(DatabaseHarness):
    def test_numbering_starts_at_101_so_ids_never_look_like_a_count(self):
        self.assertEqual(self.db.assign_ref_id("a"), 101)
        self.assertEqual(self.db.assign_ref_id("b"), 102)

    def test_assignment_is_idempotent(self):
        """A re-alert must quote the SAME number or the pointer breaks."""
        first = self.db.assign_ref_id("fp")
        self.assertEqual(self.db.assign_ref_id("fp"), first)

    def test_ids_survive_a_restart(self):
        assigned = self.db.assign_ref_id("fp")
        self.db.close()
        reopened = Database(self.path)
        try:
            self.assertEqual(reopened.assign_ref_id("fp"), assigned)
            self.assertEqual(reopened.assign_ref_id("other"), assigned + 1)
        finally:
            reopened.close()

    def test_lookup_resolves_a_reference_back_to_the_posting(self):
        job = _job()
        self.db.record_seen([job])
        ref = self.db.assign_ref_id(job.fingerprint)
        found = self.db.lookup_ref(ref)
        self.assertEqual(found["title"], "VoIP Engineer")
        self.assertEqual(found["fingerprint"], job.fingerprint)

    def test_an_unknown_reference_is_none_not_an_exception(self):
        self.assertIsNone(self.db.lookup_ref(9999))
        self.assertEqual(self.db.ref_id_for("never-seen"), 0)
        self.assertEqual(self.db.assign_ref_id(""), 0)

    def test_concurrent_assignment_yields_one_id_per_posting(self):
        """Two threads racing on the same posting must not produce two ids."""
        results: list[int] = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(self.db.assign_ref_id("contested"))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(results)), 1, results)


class TestAlertLedger(DatabaseHarness):
    def test_a_sent_alert_suppresses_the_next_one(self):
        self.db.record_alert("fp", "telegram", "sent")
        self.assertTrue(self.db.already_alerted("fp"))

    def test_a_failed_alert_does_not_block_a_retry(self):
        self.db.record_alert("fp", "callmebot", "failed", "HTTP 403")
        self.assertFalse(self.db.already_alerted("fp"))

    def test_a_dry_run_is_never_banked_as_delivered(self):
        """Banking it would permanently suppress a job the user never saw."""
        self.db.record_alert("fp", "telegram", "dry_run")
        self.assertFalse(self.db.already_alerted("fp"))

    def test_the_detail_column_is_bounded(self):
        self.db.record_alert("fp", "telegram", "failed", "x" * 5000)
        row = self.db._conn.execute("SELECT detail FROM alerts").fetchone()
        self.assertLessEqual(len(row["detail"]), 500)

    def test_cooldowns_expire(self):
        self.db.mark_cooldown("failure")
        self.assertTrue(self.db.cooldown_active("failure", 60))
        self.assertFalse(self.db.cooldown_active("failure", 0))
        self.assertFalse(self.db.cooldown_active("never-set", 60))

    def test_a_corrupt_cooldown_stamp_does_not_wedge_alerting(self):
        self.db.set_meta("cooldown:failure", "not a timestamp")
        self.assertFalse(self.db.cooldown_active("failure", 60))


class TestRetryAndHousekeeping(DatabaseHarness):
    def test_forget_un_sees_a_posting_the_ai_never_actually_judged(self):
        job = _job()
        self.db.record_seen([job])
        self.assertEqual(self.db.forget([job.fingerprint]), 1)
        fresh, _ = self.db.partition_new([_job()])
        self.assertEqual(len(fresh), 1, "a quota-failed posting was retired")

    def test_forget_never_resurrects_a_job_already_alerted(self):
        job = _job()
        self.db.record_seen([job])
        self.db.record_alert(job.fingerprint, "telegram", "sent")
        self.assertEqual(self.db.forget([job.fingerprint]), 0)

    def test_forget_tolerates_empty_and_blank_input(self):
        self.assertEqual(self.db.forget([]), 0)
        self.assertEqual(self.db.forget(["", None]), 0)

    def test_prune_drops_stale_unnotified_records_only(self):
        old = iso(utc_now() - timedelta(days=400))
        keep = _job(title="Recent")
        stale = _job(title="Ancient", url="https://x.com/old")
        alerted = _job(title="Alerted", url="https://x.com/alerted")
        self.db.record_seen([keep, stale, alerted])
        self.db.record_alert(alerted.fingerprint, "telegram", "sent")
        with self.db._tx() as c:
            c.execute("UPDATE seen_jobs SET last_seen=? WHERE fingerprint IN (?,?)",
                      (old, stale.fingerprint, alerted.fingerprint))
        self.assertEqual(self.db.prune(keep_days=180), 1)
        titles = {r["title"] for r in
                  self.db._conn.execute("SELECT title FROM seen_jobs")}
        self.assertEqual(titles, {"Recent", "Alerted"})

    def test_run_bookkeeping_records_a_full_cycle(self):
        run_id = self.db.start_run()
        self.db.finish_run(run_id, status="ok", detail="fine", scraped=10,
                           fresh=4, evaluated=4, matched=1, alerted=1)
        row = self.db._conn.execute("SELECT * FROM runs WHERE id=?",
                                    (run_id,)).fetchone()
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["scraped"], 10)
        self.assertTrue(row["finished_at"])

    def test_stats_survive_an_empty_database(self):
        stats = self.db.stats()
        self.assertEqual(stats["total_jobs_seen"], 0)
        self.assertEqual(stats["recent_runs"], [])

    def test_top_matches_for_apply_joins_the_url_and_source(self):
        """The apply engine needs both, and they live on the OTHER table."""
        job = _job()
        self.db.record_seen([job])
        self.db.record_evaluation(Evaluation(
            fingerprint=job.fingerprint, company_name="Etisalat",
            role_title="VoIP Engineer", match_score=88,
            source_platform="tanqeeb:uae", direct_link=job.url,
            skill_gaps=["No CCNA"],
        ))
        [row] = self.db.top_matches_for_apply(80)
        self.assertEqual(row["url"], job.url)
        self.assertEqual(row["source"], "tanqeeb:uae")
        self.assertEqual(json.loads(row["skill_gaps"]), ["No CCNA"])

    def test_a_low_score_is_below_the_apply_bar(self):
        job = _job()
        self.db.record_seen([job])
        self.db.record_evaluation(Evaluation(fingerprint=job.fingerprint,
                                             match_score=40))
        self.assertEqual(self.db.top_matches_for_apply(80), [])


class TestSchemaAndMigrations(DatabaseHarness):
    def test_the_schema_version_is_recorded(self):
        self.assertEqual(self.db.get_meta("schema_version"),
                         str(SCHEMA_VERSION))

    def test_the_database_is_exactly_one_file(self):
        """The cloud deployment version-controls that file, so WAL -- which
        creates -wal and -shm siblings -- would silently lose state."""
        mode = self.db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "delete")

    def test_columns_added_after_v1_are_migrated_in_place(self):
        """The live database is restored from the bot-state branch on every
        cloud run, so it predates these columns."""
        legacy = _tmp("legacy.db")
        legacy.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(legacy))
        conn.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL, match_score INTEGER NOT NULL,
                company_name TEXT, role_title TEXT, location TEXT,
                why_matched TEXT, skill_gaps TEXT, model TEXT,
                created_at TEXT NOT NULL);
        """)
        conn.commit()
        conn.close()

        upgraded = Database(legacy)
        try:
            columns = {r["name"] for r in upgraded._conn.execute(
                "PRAGMA table_info(evaluations)"
            )}
            for added, _type in db_mod.Database._ADDED_COLUMNS["evaluations"]:
                self.assertIn(added, columns, f"{added} was not migrated in")
            # And it still works afterwards.
            upgraded.record_evaluation(Evaluation(fingerprint="fp",
                                                  match_score=90, ref_id=101))
        finally:
            upgraded.close()

    def test_migration_is_idempotent(self):
        self.db.close()
        for _ in range(3):
            again = Database(self.path)
            again.close()
        self.db = Database(self.path)      # for tearDown


class TestTransactionIntegrity(DatabaseHarness):
    def test_a_failed_transaction_leaves_nothing_behind(self):
        """Half a write is worse than none: a posting recorded as seen but not
        evaluated is retired without ever being looked at."""
        before = self.db._conn.execute(
            "SELECT COUNT(*) FROM seen_jobs").fetchone()[0]
        with self.assertRaises(RuntimeError):
            with self.db._tx() as c:
                c.execute(
                    "INSERT INTO seen_jobs(fingerprint,first_seen,last_seen) "
                    "VALUES ('rollback-me', '2026-01-01', '2026-01-01')"
                )
                raise RuntimeError("something failed after the insert")
        after = self.db._conn.execute(
            "SELECT COUNT(*) FROM seen_jobs").fetchone()[0]
        self.assertEqual(after, before, "a failed transaction was committed")

    def test_a_constraint_violation_rolls_back_the_whole_statement(self):
        self.db.assign_ref_id("fp")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db._tx() as c:
                c.execute("INSERT INTO job_refs(ref_id,fingerprint,created_at) "
                          "VALUES (999,'other','2026-01-01')")
                c.execute("INSERT INTO job_refs(ref_id,fingerprint,created_at) "
                          "VALUES (999,'clash','2026-01-01')")
        self.assertIsNone(self.db.lookup_ref(999),
                          "the first insert survived a rolled-back transaction")

    def test_record_evaluation_writes_both_tables_or_neither(self):
        job = _job()
        self.db.record_seen([job])
        self.db.record_evaluation(Evaluation(fingerprint=job.fingerprint,
                                             match_score=88))
        row = self.db._conn.execute(
            "SELECT evaluated, match_score FROM seen_jobs WHERE fingerprint=?",
            (job.fingerprint,),
        ).fetchone()
        self.assertEqual((row["evaluated"], row["match_score"]), (1, 88))

    def test_writes_are_safe_from_several_threads(self):
        def worker(offset):
            self.db.record_seen([
                _job(title=f"Engineer {offset}-{i}",
                     url=f"https://x.com/{offset}/{i}")
                for i in range(20)
            ])

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        count = self.db._conn.execute(
            "SELECT COUNT(*) FROM seen_jobs").fetchone()[0]
        self.assertEqual(count, 120)


# ---------------------------------------------------------------------------
class TestVaultEncryption(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(decrypt(encrypt("Sup3rSecret!")), "Sup3rSecret!")

    def test_the_ciphertext_does_not_contain_the_plaintext(self):
        self.assertNotIn("Sup3rSecret!", encrypt("Sup3rSecret!"))

    def test_two_encryptions_of_the_same_value_differ(self):
        """Fernet includes an IV; identical ciphertext would leak equality."""
        self.assertNotEqual(encrypt("same"), encrypt("same"))

    def test_empty_values_pass_straight_through(self):
        self.assertEqual(encrypt(""), "")
        self.assertEqual(decrypt(""), "")

    def test_an_undecryptable_value_explains_itself(self):
        with self.assertRaises(VaultError) as ctx:
            decrypt("not-a-fernet-token")
        self.assertIn("vault key", str(ctx.exception))


class VaultHarness(unittest.TestCase):
    def setUp(self):
        self.path = _tmp("vault.db")
        self.v = SecureStore(self.path)

    def tearDown(self):
        self.v.close()

    def _draft(self, fingerprint="fp-1", status=STATUS_REVIEW, **kw):
        payload = kw.pop("payload", {"fields": {"#cover": "old"},
                                     "draft": {"cover_letter": "old"}})
        return self.v.record_application(
            job_fingerprint=fingerprint, job_id=101, company="Etisalat",
            role="VoIP Engineer", platform="tanqeeb:uae",
            job_url="https://uae.tanqeeb.com/1.html", payload=payload,
            cover_letter="old", status=status, **kw
        )


class TestVaultIsolation(VaultHarness):
    def test_the_vault_is_not_the_public_database(self):
        from config import settings

        self.assertNotEqual(vault_mod.DEFAULT_VAULT_PATH, settings.db_path)
        self.assertEqual(vault_mod.DEFAULT_VAULT_PATH.name, "vault.db")

    def test_the_vault_and_its_key_are_git_ignored(self):
        root = Path(__file__).resolve().parent.parent
        ignored = (root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("vault.db", ignored)
        self.assertIn("secrets/", ignored)

    def test_a_stored_password_is_not_plaintext_on_disk(self):
        self.v.save_credentials("Tanqeeb", "https://tanqeeb.com", "a@b.c",
                                "PlaintextLeak99!")
        blob = self.path.read_bytes()
        self.assertNotIn(b"PlaintextLeak99!", blob)

    def test_listing_never_decrypts(self):
        self.v.save_credentials("Tanqeeb", "u", "a@b.c", "secret")
        [row] = self.v.list_platforms()
        self.assertNotIn("password_encrypted", row)
        self.assertNotIn("password", row)

    def test_resaving_updates_rather_than_duplicating(self):
        self.v.save_credentials("Tanqeeb", "u", "a@b.c", "one")
        self.v.save_credentials("Tanqeeb", "u", "a@b.c", "two")
        self.assertEqual(len(self.v.list_platforms()), 1)
        self.assertEqual(self.v.get_credentials("Tanqeeb")["password"], "two")

    def test_an_unknown_platform_is_none(self):
        self.assertIsNone(self.v.get_credentials("NoSuchBoard"))


class TestApplicationHistoryCrud(VaultHarness):
    def test_the_fingerprint_is_the_dedup_key(self):
        first = self._draft()
        second = self._draft()
        self.assertEqual(first, second, "the same job produced two applications")

    def test_status_transitions_and_the_submission_stamp(self):
        app_id = self._draft()
        self.assertIsNone(self.v.get_application(app_id)["submitted_at"])
        self.v.set_application_status(app_id, STATUS_SUBMITTED,
                                      screenshot_path="/s/1.png")
        app = self.v.get_application(app_id)
        self.assertEqual(app["status"], STATUS_SUBMITTED)
        self.assertTrue(app["submitted_at"])

    def test_a_non_submission_never_stamps_the_time(self):
        app_id = self._draft()
        for status in (STATUS_APPROVED, STATUS_FAILED, STATUS_DECLINED):
            self.v.set_application_status(app_id, status)
            self.assertIsNone(self.v.get_application(app_id)["submitted_at"])

    def test_evidence_is_never_cleared_by_a_later_status_change(self):
        """Re-running --approve used to throw away the proof of submission."""
        app_id = self._draft()
        self.v.set_application_status(app_id, STATUS_SUBMITTED,
                                      screenshot_path="/s/proof.png")
        self.v.set_application_status(app_id, STATUS_SUBMITTED)
        self.assertEqual(self.v.get_application(app_id)["screenshot_path"],
                         "/s/proof.png")

    def test_a_stale_failure_reason_is_cleared(self):
        app_id = self._draft()
        self.v.set_application_status(app_id, STATUS_FAILED,
                                      failure_reason="no submit button")
        self.v.set_application_status(app_id, STATUS_APPROVED)
        self.assertIsNone(self.v.get_application(app_id)["failure_reason"])

    def test_unknown_ids_are_none_not_exceptions(self):
        self.assertIsNone(self.v.get_application(9999))
        self.assertIsNone(self.v.application_for_fingerprint("nope"))

    def test_company_lookup_matches_on_significant_tokens(self):
        """A recruiter writes from "Etisalat Group Careers" about an
        application stored as "Etisalat"."""
        self._draft()
        found = self.v.find_application_by_company("Etisalat Group Careers")
        self.assertIsNotNone(found)
        self.assertEqual(found["company"], "Etisalat")

    def test_company_lookup_ignores_corporate_filler(self):
        self._draft()
        for useless in ("Group Holding LLC", "Technologies", "", "the"):
            with self.subTest(needle=useless):
                self.assertIsNone(self.v.find_application_by_company(useless))


class TestDraftEditPersistence(VaultHarness):
    """The write path behind every in-line edit."""

    def test_both_halves_move_together(self):
        app_id = self._draft()
        payload = {"fields": {"#cover": "new"}, "draft": {"cover_letter": "new"}}
        self.assertTrue(self.v.update_application_draft(
            app_id, payload=payload, cover_letter="new"
        ))
        app = self.v.get_application(app_id)
        self.assertEqual(app["cover_letter_text"], "new")
        self.assertEqual(json.loads(app["submitted_payload_json"])["fields"],
                         {"#cover": "new"})

    def test_a_none_argument_leaves_that_column_alone(self):
        app_id = self._draft()
        self.v.update_application_draft(app_id, cover_letter="only the letter")
        app = self.v.get_application(app_id)
        self.assertEqual(app["cover_letter_text"], "only the letter")
        self.assertEqual(json.loads(app["submitted_payload_json"])["fields"],
                         {"#cover": "old"})

    def test_the_revision_counter_advances_on_every_edit(self):
        app_id = self._draft()
        self.assertEqual(self.v.get_application(app_id)["revision"], 0)
        for expected in (1, 2, 3):
            self.v.update_application_draft(app_id, cover_letter=f"v{expected}")
            self.assertEqual(self.v.get_application(app_id)["revision"], expected)
        self.assertTrue(self.v.get_application(app_id)["updated_at"])

    def test_editing_an_unknown_application_reports_false(self):
        self.assertFalse(self.v.update_application_draft(9999,
                                                          cover_letter="x"))

    def test_arabic_content_round_trips_without_escaping(self):
        app_id = self._draft()
        arabic = "خطاب تغطية باللغة العربية"
        self.v.update_application_draft(
            app_id, payload={"draft": {"cover_letter": arabic}},
            cover_letter=arabic,
        )
        app = self.v.get_application(app_id)
        self.assertEqual(app["cover_letter_text"], arabic)
        self.assertIn(arabic, app["submitted_payload_json"])


class TestPendingResolution(VaultHarness):
    def test_review_pending_outranks_approved(self):
        """Approving twice is harmless; submitting the wrong job is not."""
        approved = self._draft("fp-approved", status=STATUS_APPROVED)
        pending = self._draft("fp-pending", status=STATUS_REVIEW)
        self.assertGreater(pending, approved, "precondition: newer id")
        self.assertEqual(self.v.latest_pending_application()["id"], pending)

    def test_an_approved_draft_is_still_resolvable_when_nothing_is_pending(self):
        approved = self._draft("fp-approved", status=STATUS_APPROVED)
        self.assertEqual(self.v.latest_pending_application()["id"], approved)

    def test_submitted_and_declined_are_never_pending(self):
        self._draft("fp-a", status=STATUS_SUBMITTED)
        self._draft("fp-b", status=STATUS_DECLINED)
        self.assertIsNone(self.v.latest_pending_application())
        self.assertEqual(self.v.applications_awaiting_review(), [])

    def test_awaiting_review_is_newest_first_and_bounded(self):
        ids = [self._draft(f"fp-{i}") for i in range(5)]
        rows = self.v.applications_awaiting_review(limit=3)
        self.assertEqual([r["id"] for r in rows], sorted(ids, reverse=True)[:3])

    def test_an_empty_vault_resolves_to_nothing(self):
        self.assertIsNone(self.v.latest_pending_application())


class TestEmailEventLedger(VaultHarness):
    def test_message_id_is_the_anti_duplicate_key(self):
        first = self.v.record_email_event(
            message_id="<m1@x>", sender="hr@a.com", subject="Interview",
            classification="interview",
        )
        second = self.v.record_email_event(
            message_id="<m1@x>", sender="hr@a.com", subject="Interview",
            classification="interview",
        )
        self.assertEqual(first, second)
        self.assertEqual(len(self.v.recent_events()), 1)

    def test_a_conflict_returns_this_messages_id_not_the_previous_one(self):
        """`lastrowid` is meaningless after DO NOTHING -- it still points at
        whatever this cursor last inserted."""
        first = self.v.record_email_event(message_id="<a@x>", sender="a",
                                          subject="s", classification="other")
        second = self.v.record_email_event(message_id="<b@x>", sender="b",
                                           subject="s", classification="other")
        again = self.v.record_email_event(message_id="<a@x>", sender="a",
                                          subject="s", classification="other")
        self.assertEqual(again, first)
        self.assertNotEqual(again, second)

    def test_seen_message_is_the_gate(self):
        self.assertFalse(self.v.seen_message("<new@x>"))
        self.v.record_email_event(message_id="<new@x>", sender="a", subject="s",
                                 classification="not_job_related")
        self.assertTrue(self.v.seen_message("<new@x>"))

    def test_alerted_reflects_what_actually_happened(self):
        self.v.record_email_event(message_id="<m@x>", sender="a", subject="s",
                                 classification="interview", alerted=False)
        self.v.mark_event_alerted("<m@x>", True)
        self.assertEqual(self.v.recent_events()[0]["alerted"], 1)
        self.v.mark_event_alerted("<m@x>", False)
        self.assertEqual(self.v.recent_events()[0]["alerted"], 0)


class TestVaultSchemaAndMigrations(VaultHarness):
    def test_the_schema_version_is_recorded(self):
        row = self.v._conn.execute(
            "SELECT value FROM vault_meta WHERE key='schema_version'"
        ).fetchone()
        self.assertEqual(row["value"], str(VAULT_SCHEMA_VERSION))

    def test_a_v1_vault_gains_the_edit_columns_in_place(self):
        """The vault is never recreated -- it holds the only copy of every
        stored password -- so new columns must be added to the file on disk."""
        legacy = _tmp("legacy_vault.db")
        legacy.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(legacy))
        conn.executescript("""
            CREATE TABLE vault_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE applications_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_fingerprint TEXT UNIQUE NOT NULL, job_id INTEGER,
                company TEXT, role TEXT, platform TEXT, job_url TEXT,
                submitted_payload_json TEXT, cover_letter_text TEXT,
                status TEXT NOT NULL DEFAULT 'draft', screenshot_path TEXT,
                failure_reason TEXT, created_at TIMESTAMP NOT NULL,
                submitted_at TIMESTAMP);
            INSERT INTO applications_history
                (job_fingerprint, company, status, created_at, cover_letter_text)
                VALUES ('legacy-fp', 'OldCo', 'review_pending', '2026-01-01',
                        'the original letter');
        """)
        conn.commit()
        conn.close()

        upgraded = SecureStore(legacy)
        try:
            columns = {r["name"] for r in upgraded._conn.execute(
                "PRAGMA table_info(applications_history)"
            )}
            for added, _type in SecureStore._ADDED_COLUMNS["applications_history"]:
                self.assertIn(added, columns)
            # The pre-existing row must survive AND be editable.
            row = upgraded.application_for_fingerprint("legacy-fp")
            self.assertEqual(row["cover_letter_text"], "the original letter")
            self.assertTrue(upgraded.update_application_draft(
                row["id"], cover_letter="edited after migration"
            ))
            self.assertEqual(
                upgraded.get_application(row["id"])["revision"], 1,
                "revision must start from 0 on a migrated row, not NULL",
            )
        finally:
            upgraded.close()

    def test_stats_report_both_ledgers(self):
        self._draft("fp-a", status=STATUS_SUBMITTED)
        self._draft("fp-b", status=STATUS_REVIEW)
        self.v.record_email_event(message_id="<m@x>", sender="a", subject="s",
                                 classification="interview")
        stats = self.v.stats()
        self.assertEqual(stats["applications"], 2)
        self.assertEqual(stats["applications_by_status"][STATUS_SUBMITTED], 1)
        self.assertEqual(stats["email_events"], 1)
        self.assertEqual(stats["events_by_class"]["interview"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
