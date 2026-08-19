"""
Dual-channel alerting: short reference IDs, and the two card formats.

The WhatsApp card deliberately carries NO application URL -- job links run to
400+ characters, percent-encode badly, and CallMeBot drops what overflows its
query string rather than truncating. So the short reference (#101) is the ONLY
route from a WhatsApp alert back to the job, which makes ID stability a
correctness property rather than a nicety.

Run:  python tests/test_dual_channel.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("CALLMEBOT_APIKEY", "test-apikey")
os.environ.setdefault("WHATSAPP_PHONE", "+201234567890")
os.environ.setdefault("CV_TEXT", "VoIP engineer with SIP and Issabel PBX. " * 6)
os.environ.setdefault("DRY_RUN", "true")

from db import Database                                    # noqa: E402
from models import Evaluation, JobPost, _split_gaps        # noqa: E402
from notifier import MAX_URL_LENGTH, WhatsAppNotifier      # noqa: E402


def _encoded_url(text: str) -> str:
    return ("https://api.callmebot.com/whatsapp.php"
            f"?phone=%2B201234567890&apikey=TESTKEY&text={quote(text)}")


class TestShortReferenceIds(unittest.TestCase):
    """#101 is the only way back to the job from a WhatsApp alert."""

    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "refs.db")

    def tearDown(self):
        self.db.close()

    def test_numbering_starts_at_101_and_increments(self):
        self.assertEqual(self.db.assign_ref_id("a"), 101)
        self.assertEqual(self.db.assign_ref_id("b"), 102)
        self.assertEqual(self.db.assign_ref_id("c"), 103)

    def test_assignment_is_idempotent(self):
        """A re-alert must quote the SAME number or the pointer breaks."""
        first = self.db.assign_ref_id("stable")
        for _ in range(5):
            self.assertEqual(self.db.assign_ref_id("stable"), first)

    def test_ids_survive_a_restart(self):
        path = Path(tempfile.mkdtemp()) / "persist.db"
        db1 = Database(path)
        first = db1.assign_ref_id("x")
        db1.close()

        db2 = Database(path)
        try:
            self.assertEqual(db2.assign_ref_id("x"), first)
            self.assertEqual(db2.assign_ref_id("y"), first + 1,
                             "numbering must continue, not restart")
        finally:
            db2.close()

    def test_lookup_resolves_a_reference_back_to_the_job(self):
        job = JobPost(source="linkedin", title="VoIP Engineer", company="Etisalat",
                      location="Dubai", url="https://example.com/1")
        fresh, _ = self.db.partition_new([job])
        self.db.record_seen(fresh)
        ref = self.db.assign_ref_id(job.fingerprint)

        found = self.db.lookup_ref(ref)
        self.assertIsNotNone(found)
        self.assertEqual(found["title"], "VoIP Engineer")

    def test_unknown_lookups_are_safe(self):
        self.assertIsNone(self.db.lookup_ref(999999))
        self.assertEqual(self.db.ref_id_for("never-seen"), 0)
        self.assertEqual(self.db.assign_ref_id(""), 0)


class TestDualChannelCards(unittest.TestCase):
    """Two channels, two different cards, one shared reference."""

    def setUp(self):
        self.n = WhatsAppNotifier(None)

    @staticmethod
    def _eval(**kw) -> Evaluation:
        base = dict(
            fingerprint="fp", ref_id=101, company_name="Etisalat",
            role_title="VoIP Support Engineer", location="Dubai, UAE",
            salary="12,000-15,000 AED per month", match_score=95,
            source_platform="linkedin",
            direct_link="https://ae.linkedin.com/jobs/view/voip-engineer-4455251103",
            why_matched="Matches Asterisk and Issabel PBX administration.",
            skill_gaps=["Under 2 years of experience"],
            arabic_summary="مهندس دعم شبكات VoIP لإدارة السنترالات",
            why_matched_ar="خبرة ممتازة في Asterisk وIssabel وتصميم الـ IVR",
            gaps_ar="سنوات الخبرة أقل من المطلوب",
        )
        base.update(kw)
        return Evaluation(**base)

    # -- WhatsApp card ------------------------------------------------------
    def test_whatsapp_card_never_carries_a_url(self):
        msg = self.n.format_whatsapp_card(self._eval(), telegram_delivered=True)
        self.assertNotIn("http", msg)
        self.assertIn("Search Telegram Saved Messages for #101", msg)

    def test_whatsapp_card_fits_the_encoded_budget_with_arabic(self):
        msg = self.n.format_whatsapp_card(self._eval())
        self.assertLessEqual(len(_encoded_url(msg)), MAX_URL_LENGTH)

    def test_whatsapp_card_carries_the_metadata(self):
        msg = self.n.format_whatsapp_card(self._eval())
        for needle in ("Etisalat", "VoIP Support Engineer", "Dubai", "95%",
                       "12,000-15,000 AED"):
            self.assertIn(needle, msg)

    def test_whatsapp_card_carries_the_arabic_lines(self):
        msg = self.n.format_whatsapp_card(self._eval())
        for needle in ("مهندس دعم", "خبرة ممتازة", "سنوات الخبرة"):
            self.assertIn(needle, msg)

    def test_whatsapp_falls_back_to_the_url_when_telegram_failed(self):
        """A pointer at a card that was never delivered is a dead end."""
        msg = self.n.format_whatsapp_card(self._eval(), telegram_delivered=False)
        self.assertIn("linkedin.com/jobs/view/voip-engineer", msg)
        self.assertNotIn("Saved Messages", msg)

    def test_overlong_arabic_is_shrunk_never_at_the_cost_of_metadata(self):
        msg = self.n.format_whatsapp_card(self._eval(
            arabic_summary="ا" * 500, why_matched_ar="ب" * 500, gaps_ar="ج" * 500,
        ))
        self.assertLessEqual(len(_encoded_url(msg)), MAX_URL_LENGTH)
        self.assertIn("#101", msg)
        self.assertIn("Etisalat", msg, "metadata must never be sacrificed")
        self.assertIn("95%", msg)

    def test_salary_line_disappears_when_unknown(self):
        self.assertNotIn("Salary", self.n.format_whatsapp_card(self._eval(salary="")))
        self.assertNotIn("Salary", self.n.format_telegram_card(self._eval(salary="")))

    # -- Telegram card ------------------------------------------------------
    def test_telegram_card_carries_link_and_english_detail(self):
        msg = self.n.format_telegram_card(self._eval())
        self.assertIn("linkedin.com/jobs/view/voip-engineer", msg)
        self.assertIn("Why you match", msg)
        self.assertIn("Under 2 years", msg)

    def test_telegram_card_carries_both_languages(self):
        msg = self.n.format_telegram_card(self._eval())
        self.assertIn("Asterisk", msg)
        self.assertIn("مهندس دعم", msg)

    def test_telegram_card_stays_within_telegram_limit(self):
        msg = self.n.format_telegram_card(self._eval(
            why_matched="x" * 3000, skill_gaps=["y" * 500] * 8,
            arabic_summary="ا" * 600,
        ))
        self.assertLessEqual(len(msg), 4096)

    def test_missing_link_is_explicit_rather_than_blank(self):
        msg = self.n.format_telegram_card(self._eval(direct_link=""))
        self.assertIn("no direct link", msg)

    # -- shared reference ---------------------------------------------------
    def test_reference_is_identical_on_both_cards(self):
        ev = self._eval(ref_id=417)
        self.assertIn("#417", self.n.format_whatsapp_card(ev))
        self.assertIn("#417", self.n.format_telegram_card(ev))

    def test_missing_reference_degrades_visibly(self):
        """A card with no id must not claim to be searchable by one."""
        self.assertIn("#--", self.n.format_whatsapp_card(self._eval(ref_id=0)))


class TestDispatchAssignsAndRecords(unittest.TestCase):
    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "dispatch.db")
        self.n = WhatsAppNotifier(self.db)
        self.n.dry_run = True  # no network

    def tearDown(self):
        self.db.close()

    def test_dispatch_assigns_a_reference(self):
        ev = TestDualChannelCards._eval(ref_id=0, fingerprint="fp-dispatch")
        self.n.dispatch([ev])
        self.assertGreaterEqual(ev.ref_id, 101)

    def test_dry_run_is_not_banked_as_delivered(self):
        ev = TestDualChannelCards._eval(ref_id=0, fingerprint="fp-dry")
        self.n.dispatch([ev])
        self.assertFalse(
            self.db.already_alerted("fp-dry"),
            "a dry run recorded as sent would suppress the real alert",
        )

    def test_second_dispatch_reuses_the_same_reference(self):
        ev = TestDualChannelCards._eval(ref_id=0, fingerprint="fp-same")
        self.n.dispatch([ev])
        first = ev.ref_id

        again = TestDualChannelCards._eval(ref_id=0, fingerprint="fp-same")
        self.n.dispatch([again])
        self.assertEqual(again.ref_id, first)


class TestGapSplitting(unittest.TestCase):
    """Gemini writes explanatory clauses; naive comma-splitting mangles them."""

    def test_explanatory_clause_stays_one_gap(self):
        out = _split_gaps(
            "Requires 2+ years of experience, whereas the candidate has 1 year"
        )
        self.assertEqual(len(out), 1, f"clause was split into fragments: {out}")

    def test_genuine_list_splits(self):
        self.assertEqual(_split_gaps("Kubernetes, Docker, Terraform"),
                         ["Kubernetes", "Docker", "Terraform"])

    def test_single_item_and_empty(self):
        self.assertEqual(_split_gaps("Cisco CCNA"), ["Cisco CCNA"])
        self.assertEqual(_split_gaps(""), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
