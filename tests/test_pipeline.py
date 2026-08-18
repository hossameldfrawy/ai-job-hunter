"""
Offline test suite -- no network, no API keys, no cost.

Covers the logic where a silent bug is most expensive:
  * fingerprinting  -> a bad one spams the user with duplicates
  * dedup           -> the promise of "never twice" must hold across restarts
  * pre-filter      -> a wrong disqualification hides real jobs forever
  * alert format    -> must fit CallMeBot's URL budget and stay readable
  * Gemini parsing  -> malformed AI output must never crash a run

Run:  python -m pytest tests/ -v      (or: python tests/test_pipeline.py)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Satisfy config validation before importing modules that read settings.
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("CALLMEBOT_APIKEY", "test-apikey")
os.environ.setdefault("WHATSAPP_PHONE", "+201234567890")
os.environ.setdefault("CV_TEXT", "Test CV. VoIP engineer with SIP and Issabel PBX experience. " * 5)
os.environ.setdefault("DRY_RUN", "true")

from config import settings                      # noqa: E402
from db import Database                          # noqa: E402
from models import (                             # noqa: E402
    Evaluation, JobPost, canonical_url, fingerprint, normalise_text, utc_now,
)
from notifier import WhatsAppNotifier            # noqa: E402
from relevance import prefilter, score_job       # noqa: E402
from scrapers.base import parse_date, first_url, strip_html  # noqa: E402


def job(**kw) -> JobPost:
    base = dict(source="test", title="VoIP Engineer", company="Etisalat",
                location="Dubai, UAE", url="https://example.com/job/1",
                description="SIP, Asterisk, Issabel PBX support.")
    base.update(kw)
    return JobPost(**base)


class TestNormalisation(unittest.TestCase):
    def test_arabic_survives_normalisation(self):
        # Arabic must NOT be stripped -- a large share of Gulf postings use it.
        out = normalise_text("مطلوب مهندس دعم فني")
        self.assertTrue(out.strip(), "Arabic text was destroyed by normalisation")
        self.assertIn("مهندس", out)

    def test_case_and_punctuation_folded(self):
        self.assertEqual(normalise_text("VoIP  Engineer!!"), "voip engineer")

    def test_canonical_url_strips_tracking(self):
        dirty = "https://WWW.Example.com/job/1/?utm_source=x&trk=y&id=7"
        self.assertEqual(canonical_url(dirty), "https://example.com/job/1?id=7")

    def test_canonical_url_handles_junk(self):
        self.assertEqual(canonical_url(""), "")
        self.assertEqual(canonical_url(None), "")
        self.assertEqual(canonical_url("not a url"), "not a url")


class TestFingerprint(unittest.TestCase):
    def test_same_job_same_fingerprint(self):
        a = fingerprint("Etisalat", "VoIP Engineer", "Dubai, UAE")
        b = fingerprint("etisalat", "VOIP  engineer", "Dubai, UAE")
        self.assertEqual(a, b)

    def test_recruiter_noise_ignored(self):
        # These are the SAME posting dressed up differently.
        a = fingerprint("Etisalat", "VoIP Engineer", "Dubai")
        b = fingerprint("Etisalat", "URGENT Hiring: VoIP Engineer", "Dubai")
        self.assertEqual(a, b, "recruiter filler changed the identity")

    def test_location_granularity(self):
        a = fingerprint("Etisalat", "VoIP Engineer", "Dubai, Dubai, UAE")
        b = fingerprint("Etisalat", "VoIP Engineer", "Dubai, UAE")
        self.assertEqual(a, b)

    def test_different_jobs_differ(self):
        a = fingerprint("Etisalat", "VoIP Engineer", "Dubai")
        b = fingerprint("Du", "VoIP Engineer", "Dubai")
        self.assertNotEqual(a, b)

    def test_falls_back_to_url_without_title(self):
        fp = fingerprint("", "", "", "https://example.com/x")
        self.assertTrue(fp)


class TestDeduplication(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(Path(self.tmp) / "test.db")

    def tearDown(self):
        self.db.close()

    def test_new_jobs_pass_once_then_never_again(self):
        jobs = [job(), job(title="IT Support Engineer", url="https://example.com/2")]
        fresh, dupes = self.db.partition_new(jobs)
        self.assertEqual(len(fresh), 2)
        self.assertEqual(dupes, 0)

        self.db.record_seen(fresh)

        fresh2, dupes2 = self.db.partition_new(jobs)
        self.assertEqual(len(fresh2), 0, "a previously-seen job came back as new")
        self.assertEqual(dupes2, 2)

    def test_survives_a_restart(self):
        """The core promise: state outlives the process."""
        path = Path(self.tmp) / "restart.db"
        db1 = Database(path)
        fresh, _ = db1.partition_new([job()])
        db1.record_seen(fresh)
        db1.close()

        db2 = Database(path)
        try:
            fresh2, dupes = db2.partition_new([job()])
            self.assertEqual(len(fresh2), 0)
            self.assertEqual(dupes, 1)
        finally:
            db2.close()

    def test_intra_batch_duplicates_collapse(self):
        # Same role scraped from LinkedIn AND Telegram in one run.
        a = job(source="linkedin")
        b = job(source="telegram:x", url="https://t.me/x/1")
        fresh, dupes = self.db.partition_new([a, b])
        self.assertEqual(len(fresh), 1, "syndicated duplicate was not collapsed")
        self.assertEqual(dupes, 1)

    def test_alert_recorded_once(self):
        fp = job().fingerprint
        self.assertFalse(self.db.already_alerted(fp))
        self.db.record_alert(fp, "callmebot", "sent")
        self.assertTrue(self.db.already_alerted(fp))

    def test_failed_alert_does_not_block_retry(self):
        fp = job().fingerprint
        self.db.record_alert(fp, "callmebot", "failed", "network error")
        self.assertFalse(
            self.db.already_alerted(fp),
            "a FAILED send must not be treated as delivered",
        )

    def test_cooldown(self):
        self.assertFalse(self.db.cooldown_active("failure", 60))
        self.db.mark_cooldown("failure")
        self.assertTrue(self.db.cooldown_active("failure", 60))


class TestPrefilter(unittest.TestCase):
    def setUp(self):
        self.profile = settings.profile

    def test_core_role_scores_high(self):
        score, _ = score_job(job(), self.profile)
        self.assertGreater(score, 5)

    def test_negative_keyword_disqualifies(self):
        score, why = score_job(
            job(title="Registered Nurse", description="ICU nursing"), self.profile
        )
        self.assertLess(score, -10)
        self.assertEqual(why.get("reason"), "negative_keyword")

    def test_executive_role_disqualified(self):
        score, why = score_job(
            job(title="Chief Technology Officer", description="Lead engineering"),
            self.profile,
        )
        self.assertLess(score, -10)
        self.assertEqual(why.get("reason"), "seniority_mismatch")

    def test_word_boundary_prevents_false_positive(self):
        """'sip' must not match inside 'gossip' -- this bug floods the pipeline."""
        score, _ = score_job(
            job(title="Gossip Columnist", company="Tabloid", location="London",
                description="Write celebrity gossip."),
            self.profile,
        )
        self.assertLess(score, 2)

    def test_prefilter_sorts_best_first(self):
        jobs = [
            job(title="Office Assistant", description="Filing.", url="https://e.com/1"),
            job(title="VoIP SIP Asterisk Engineer",
                description="Issabel PBX IVR telephony Dubai", url="https://e.com/2"),
        ]
        kept, _weak, _dq = prefilter(jobs, self.profile, minimum=1)
        self.assertTrue(kept)
        self.assertIn("VoIP", kept[0].title)

    def test_arabic_posting_is_not_disqualified(self):
        arabic = job(
            title="مطلوب مهندس دعم فني",
            company="", location="",
            description="مطلوب مهندس دعم تطبيقات خبرة SIP و Issabel و Odoo القاهرة",
        )
        score, _ = score_job(arabic, self.profile)
        self.assertGreaterEqual(score, 2, "Arabic posting was wrongly filtered out")


class TestAgeHandling(unittest.TestCase):
    def test_age_days(self):
        old = job(posted_at=utc_now() - timedelta(days=30))
        self.assertGreater(old.age_days, 29)

    def test_unknown_age_is_none(self):
        self.assertIsNone(job(posted_at=None).age_days)

    def test_age_gate_keeps_undated_postings(self):
        """Telegram rarely dates posts; dropping them would kill the source."""
        from pipeline import _age_gate

        kept, dropped = _age_gate(
            [job(posted_at=None), job(posted_at=utc_now() - timedelta(days=90),
                                      url="https://e.com/old")],
            max_days=21,
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 1)


class TestDateParsing(unittest.TestCase):
    def test_formats(self):
        for value in [
            "2026-08-18", "2026-08-18T10:30:00Z", "2026-08-18T10:30:00+00:00",
            "Mon, 18 Aug 2026 10:30:00 +0000", 1755511800, "18 Aug 2026",
        ]:
            self.assertIsNotNone(parse_date(value), f"failed to parse {value!r}")

    def test_relative(self):
        self.assertIsNotNone(parse_date("3 days ago"))

    def test_garbage_returns_none(self):
        for value in ["", None, "not a date", {}]:
            self.assertIsNone(parse_date(value))


class TestHtmlHelpers(unittest.TestCase):
    def test_strip_html(self):
        self.assertEqual(strip_html("<p>Hello <b>world</b></p>"), "Hello world")

    def test_entities(self):
        self.assertEqual(strip_html("<p>R&amp;D</p>"), "R&D")

    def test_first_url_trims_punctuation(self):
        self.assertEqual(
            first_url("Apply at https://example.com/job."), "https://example.com/job"
        )


class TestAlertFormatting(unittest.TestCase):
    def setUp(self):
        self.notifier = WhatsAppNotifier(None)

    def _eval(self, **kw) -> Evaluation:
        base = dict(
            fingerprint="abc", company_name="Etisalat", role_title="VoIP Engineer",
            location="Dubai, UAE", match_score=88, source_platform="linkedin",
            direct_link="https://linkedin.com/jobs/view/1",
            why_matched="Requires Asterisk and SIP trunk troubleshooting.",
            skill_gaps=["Cisco certification"],
        )
        base.update(kw)
        return Evaluation(**base)

    def test_contains_every_required_field(self):
        msg = self.notifier.format_alert(self._eval())
        for needle in ["88%", "Etisalat", "Dubai", "VoIP Engineer",
                       "linkedin.com/jobs/view/1", "Why You Match"]:
            self.assertIn(needle, msg)

    def test_uses_whatsapp_single_asterisk_bold(self):
        msg = self.notifier.format_alert(self._eval())
        self.assertIn("*Company:*", msg)
        self.assertNotIn("**", msg, "markdown ** does not render in WhatsApp")

    def test_long_content_fits_the_url_budget(self):
        msg = self.notifier.format_alert(self._eval(
            why_matched="x" * 3000,
            skill_gaps=["y" * 400, "z" * 400],
        ))
        self.assertLessEqual(len(msg), 1000)
        # The link must survive truncation -- it is the whole point of the alert.
        self.assertIn("linkedin.com/jobs/view/1", msg)

    def test_missing_link_is_explicit(self):
        msg = self.notifier.format_alert(self._eval(direct_link=""))
        self.assertIn("no direct link", msg)

    def test_response_classification(self):
        ok, _ = WhatsAppNotifier._classify("<p>Message queued.</p>")
        self.assertTrue(ok)
        bad, detail = WhatsAppNotifier._classify("<p>APIKey is invalid</p>")
        self.assertFalse(bad)
        self.assertIn("wrong", detail.lower())


class TestEvaluationParsing(unittest.TestCase):
    def test_link_and_source_come_from_the_job_not_the_model(self):
        """The model must never be able to invent a link."""
        j = job()
        ev = Evaluation.from_gemini(
            {"company_name": "X", "role_title": "Y", "location": "Z",
             "match_score": 80, "why_matched": "w", "skill_gaps": [],
             "direct_link": "https://evil-hallucinated.example",
             "source_platform": "made-up"},
            j, "gemini-test",
        )
        self.assertEqual(ev.direct_link, j.url)
        self.assertEqual(ev.source_platform, j.source)

    def test_score_is_clamped(self):
        for raw, want in ((999, 100), (-50, 0), ("87", 87), ("bad", 0), (None, 0)):
            ev = Evaluation.from_gemini({"match_score": raw}, job(), "m")
            self.assertEqual(ev.match_score, want, f"score {raw!r}")

    def test_skill_gaps_accepts_a_string(self):
        ev = Evaluation.from_gemini({"skill_gaps": "Cisco, Juniper"}, job(), "m")
        self.assertEqual(ev.skill_gaps, ["Cisco", "Juniper"])

    def test_missing_fields_fall_back_to_the_job(self):
        ev = Evaluation.from_gemini({}, job(), "m")
        self.assertEqual(ev.company_name, "Etisalat")
        self.assertEqual(ev.role_title, "VoIP Engineer")


class TestTelegramParsing(unittest.TestCase):
    def test_title_derivation_skips_emoji_and_contacts(self):
        from scrapers.telegram_web import TelegramWebScraper

        text = "\U0001F525\U0001F525\U0001F525\nSenior VoIP Engineer\nللتواصل واتس 00201062340396"
        self.assertEqual(
            TelegramWebScraper._derive_title(text), "Senior VoIP Engineer"
        )

    def test_title_falls_back_when_everything_is_noise(self):
        from scrapers.telegram_web import TelegramWebScraper

        self.assertTrue(TelegramWebScraper._derive_title("\U0001F525 \U0001F525"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
