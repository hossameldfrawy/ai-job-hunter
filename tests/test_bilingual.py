"""
Bilingual (Arabic + English) matching, CV fallback, and Tanqeeb guards.

These cover the parts where a bug is SILENT -- the pipeline keeps running, the
logs look healthy, and Arabic postings simply never match. That failure mode is
invisible without tests, so it gets the most of them.

Run:  python tests/test_bilingual.py     (or: python -m pytest tests/ -v)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("CALLMEBOT_APIKEY", "test-apikey")
os.environ.setdefault("WHATSAPP_PHONE", "+201234567890")
os.environ.setdefault("CV_TEXT", "VoIP engineer with SIP and Issabel PBX experience. " * 6)
os.environ.setdefault("DRY_RUN", "true")

from config import settings                                    # noqa: E402
from models import (                                           # noqa: E402
    JobPost, fingerprint, normalise_text, utc_now,
)
from relevance import _compile_terms, score_job                # noqa: E402


def _matches(term: str, text: str) -> bool:
    return bool(_compile_terms((term,))[0].search(normalise_text(text)))


class TestArabicNormalisation(unittest.TestCase):
    """Arabic has several ways to write the same word. All must fold together."""

    def test_alef_variants_fold(self):
        # أ إ آ ا all appear in real postings for the same word.
        forms = ["أخصائي", "اخصائي", "إخصائي"]
        normalised = {normalise_text(f) for f in forms}
        self.assertEqual(len(normalised), 1, f"alef variants did not fold: {normalised}")

    def test_teh_marbuta_folds_to_heh(self):
        self.assertEqual(normalise_text("تقنية"), normalise_text("تقنيه"))

    def test_alef_maksura_folds_to_yeh(self):
        self.assertEqual(normalise_text("فنى"), normalise_text("فني"))

    def test_harakat_are_stripped(self):
        self.assertEqual(normalise_text("مُهَنْدِس"), normalise_text("مهندس"))

    def test_tatweel_is_stripped(self):
        self.assertEqual(normalise_text("ســـنترالات"), normalise_text("سنترالات"))

    def test_arabic_is_not_deleted(self):
        """The whole language must survive normalisation."""
        self.assertTrue(normalise_text("مطلوب مهندس شبكات").strip())

    def test_english_is_unaffected(self):
        self.assertEqual(normalise_text("VoIP  Engineer!!"), "voip engineer")

    def test_mixed_script_line(self):
        out = normalise_text("مطلوب VoIP Engineer خبرة SIP")
        self.assertIn("voip", out)
        self.assertIn("sip", out)
        self.assertIn("مطلوب", out)


class TestBilingualWordBoundaries(unittest.TestCase):
    """`sip` must not match `gossip`; `شبكات` must not match `الشبكاتيون`."""

    def test_latin_boundary_blocks_substring(self):
        self.assertFalse(_matches("sip", "celebrity gossip column"))
        self.assertTrue(_matches("sip", "SIP trunk configuration"))

    def test_arabic_boundary_blocks_substring(self):
        self.assertFalse(_matches("شبكات", "الشبكاتيون"))
        self.assertTrue(_matches("شبكات", "مهندس شبكات"))

    def test_terms_are_normalised_before_compiling(self):
        """Regression: a term with ة could never match its own subject.

        `score_text` normalises the haystack, so a raw term keeping ة/ى/أ was
        compiled into a pattern that could not match the folded text. English
        terms were unaffected, which is why this hid so well.
        """
        self.assertTrue(_matches("تقنية معلومات", "مطلوب خريج تقنية معلومات"))
        self.assertTrue(_matches("أنظمة", "مهندس أنظمة"))
        self.assertTrue(_matches("انظمة", "مهندس أنظمة"))

    def test_symbol_edged_terms_still_match(self):
        """No guard on non-letter edges, or `3cx` would break."""
        self.assertTrue(_matches("3cx", "3CX PBX administration"))

    def test_multiword_terms_tolerate_extra_whitespace(self):
        self.assertTrue(_matches("it support", "IT    support engineer"))
        self.assertTrue(_matches("دعم فني", "مطلوب دعم   فني"))


class TestArabicJobScoring(unittest.TestCase):
    """An Arabic posting must reach Gemini, not die in the pre-filter."""

    def _job(self, title, desc="", loc=""):
        return JobPost(source="tanqeeb:egypt", title=title, company="",
                       location=loc, url="https://example.com/1", description=desc)

    def test_arabic_support_role_passes(self):
        score, _ = score_job(
            self._job("مطلوب اخصائي دعم فني",
                      "خبرة في الشبكات وأنظمة التشغيل والسنترالات", "القاهرة"),
            settings.profile,
        )
        self.assertGreaterEqual(score, 2, "Arabic IT-support post was filtered out")

    def test_arabic_network_engineer_passes(self):
        score, _ = score_job(
            self._job("مهندس شبكات", "خبرة سيسكو ولينكس", "الرياض"),
            settings.profile,
        )
        self.assertGreaterEqual(score, 2)

    def test_arabic_location_scores(self):
        score, why = score_job(
            self._job("مهندس اتصالات", "سنترالات", "الدوحة"), settings.profile
        )
        self.assertGreaterEqual(score, 2)

    def test_arabic_irrelevant_role_is_not_boosted(self):
        score, _ = score_job(
            self._job("مطلوب سائق توصيل", "رخصة قيادة", "القاهرة"), settings.profile
        )
        self.assertLess(score, 2)

    def test_english_still_scores(self):
        score, _ = score_job(
            self._job("VoIP Engineer", "Asterisk, Issabel, SIP", "Dubai"),
            settings.profile,
        )
        self.assertGreater(score, 5)


class TestArabicFingerprint(unittest.TestCase):
    def test_spelling_variants_are_the_same_job(self):
        """Two boards spelling the same Arabic role differently = ONE alert."""
        a = fingerprint("شركة النور", "اخصائي دعم فني", "القاهرة")
        b = fingerprint("شركة النور", "أخصائي دعم فنى", "القاهرة")
        self.assertEqual(a, b, "orthographic variants produced two fingerprints")

    def test_distinct_arabic_roles_differ(self):
        a = fingerprint("شركة النور", "مهندس شبكات", "القاهرة")
        b = fingerprint("شركة النور", "محاسب", "القاهرة")
        self.assertNotEqual(a, b)


class TestTanqeebGuards(unittest.TestCase):
    """Tanqeeb answers a zero-result search with unrelated filler."""

    def setUp(self):
        from scrapers.tanqeeb import TanqeebScraper

        self.cls = TanqeebScraper

    def test_relevant_card_is_kept(self):
        tokens = self.cls._query_tokens("it support")
        self.assertTrue(self.cls._is_relevant("IT Support Engineer at Ninja", tokens))

    def test_filler_card_is_rejected(self):
        """Searching `voip` really does return "Governess" -- verified live."""
        tokens = self.cls._query_tokens("voip")
        self.assertFalse(self.cls._is_relevant("Governess - Bold Line UAE", tokens))
        self.assertFalse(self.cls._is_relevant("Reservation Agent", tokens))

    def test_arabic_query_tokens(self):
        tokens = self.cls._query_tokens("دعم فني")
        # "فني" is a stopword here: far too generic to prove relevance.
        self.assertIn(normalise_text("دعم"), tokens)
        self.assertTrue(self.cls._is_relevant("اخصائي دعم فني", tokens))

    def test_generic_tokens_do_not_prove_relevance(self):
        tokens = self.cls._query_tokens("it support")
        self.assertNotIn("it", tokens, "'it' is too generic to be a relevance token")

    def test_short_query_still_yields_a_token(self):
        self.assertTrue(self.cls._query_tokens("hr"))


class TestCVFallbackChain(unittest.TestCase):
    """A missing primary CV must fall through, not fail the run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.good = self.tmp / "fallback.txt"
        # Must clear the 120-character minimum that `_from_path` enforces --
        # anything shorter is treated as a failed extraction, by design.
        self.good.write_text(
            "Hossam Eldefrawy. IT Application Support Engineer, Cairo, Egypt. "
            "VoIP and telephony: SIP, IAX2, Issabel PBX, IVR design, softphone "
            "deployment. Also Odoo ITSM, Python automation, POS and CCTV. "
            "Open to relocation across the GCC.", encoding="utf-8",
        )
        self._orig = settings.raw.get("cv", {}).get("paths")

    def tearDown(self):
        if self._orig is not None:
            settings.raw.setdefault("cv", {})["paths"] = self._orig
        import cv_profile

        cv_profile._cached = None

    def _load(self, paths):
        import cv_profile

        settings.raw.setdefault("cv", {})["paths"] = paths
        cv_profile._cached = None
        os.environ.pop("CV_TEXT", None)
        try:
            return cv_profile._from_path()
        finally:
            os.environ["CV_TEXT"] = "restored for other tests " * 10

    def test_missing_primary_falls_through_to_fallback(self):
        profile = self._load([str(self.tmp / "nope.pdf"), str(self.good)])
        self.assertIsNotNone(profile, "fallback path was not used")
        self.assertIn("Issabel", profile.text)

    def test_first_usable_path_wins(self):
        first = self.tmp / "primary.txt"
        first.write_text("PRIMARY CV. VoIP engineer, SIP and Asterisk, Cairo. " * 4,
                         encoding="utf-8")
        profile = self._load([str(first), str(self.good)])
        self.assertIn("PRIMARY", profile.text)

    def test_exhausted_chain_returns_none(self):
        self.assertIsNone(self._load([str(self.tmp / "a.pdf"), str(self.tmp / "b.pdf")]))

    def test_too_short_file_is_skipped(self):
        stub = self.tmp / "stub.txt"
        stub.write_text("hi", encoding="utf-8")
        profile = self._load([str(stub), str(self.good)])
        self.assertIn("Issabel", profile.text)


class TestSecretExportLimit(unittest.TestCase):
    """GitHub rejects a secret over 64 KB; fail loudly, not at `gh secret set`."""

    def test_oversized_export_raises(self):
        import cv_profile
        from cv_profile import CVError, CVProfile

        original = cv_profile.load_cv
        cv_profile.load_cv = lambda *a, **k: CVProfile(  # type: ignore[assignment]
            text="x" * 70_000, source="test", chars=70_000
        )
        try:
            with self.assertRaises(CVError) as ctx:
                cv_profile.export_secret(Path(tempfile.mkdtemp()) / "CV_TEXT.txt")
            self.assertIn("64", str(ctx.exception))
        finally:
            cv_profile.load_cv = original

    def test_normal_export_succeeds(self):
        import cv_profile
        from cv_profile import CVProfile

        original = cv_profile.load_cv
        cv_profile.load_cv = lambda *a, **k: CVProfile(  # type: ignore[assignment]
            text="VoIP engineer CV. " * 100, source="test", chars=1800
        )
        try:
            target = Path(tempfile.mkdtemp()) / "CV_TEXT.txt"
            path, size = cv_profile.export_secret(target)
            self.assertTrue(path.exists())
            self.assertLess(size, 65536)
        finally:
            cv_profile.load_cv = original


class TestFailedEvaluationsAreRetried(unittest.TestCase):
    """A posting the AI never actually judged must not be retired.

    Postings are recorded as seen BEFORE Gemini runs, which is what makes a
    mid-run crash safe. But when a batch fails for a transport reason -- an
    exhausted daily quota, a dead model -- those postings were never judged, and
    leaving them recorded would silently discard them forever. `db.forget()`
    puts them back in the queue.
    """

    def setUp(self):
        from db import Database

        self.db = Database(Path(tempfile.mkdtemp()) / "retry.db")
        self.jobs = [
            JobPost(source="tanqeeb:saudi", title=f"IT Support Engineer {i}",
                    company="Acme", location="Riyadh",
                    url=f"https://example.com/{i}")
            for i in range(5)
        ]

    def tearDown(self):
        self.db.close()

    def test_forgotten_jobs_come_back_as_new(self):
        fresh, _ = self.db.partition_new(self.jobs)
        self.db.record_seen(fresh)
        self.assertEqual(len(self.db.partition_new(self.jobs)[0]), 0)

        self.db.forget([j.fingerprint for j in self.jobs[:3]])
        again, _ = self.db.partition_new(self.jobs)
        self.assertEqual(len(again), 3, "un-seen postings were not re-queued")

    def test_alerted_jobs_are_never_forgotten(self):
        """Safety rail: forgetting a job we already messaged about would
        re-alert the user on the next run."""
        fresh, _ = self.db.partition_new(self.jobs)
        self.db.record_seen(fresh)
        self.db.record_alert(self.jobs[0].fingerprint, "callmebot", "sent")

        self.db.forget([j.fingerprint for j in self.jobs])
        again, _ = self.db.partition_new(self.jobs)
        surviving = {j.fingerprint for j in again}
        self.assertNotIn(self.jobs[0].fingerprint, surviving,
                         "an already-alerted job was un-seen and would re-alert")
        self.assertEqual(len(again), 4)

    def test_forget_handles_empty_input(self):
        self.assertEqual(self.db.forget([]), 0)
        self.assertEqual(self.db.forget(["", None]), 0)  # type: ignore[list-item]


class TestQuotaExhaustion(unittest.TestCase):
    """An exhausted daily quota must fail fast, not stall the whole run."""

    def test_quota_error_is_its_own_type(self):
        from evaluator import GeminiError, QuotaExhausted

        self.assertTrue(issubclass(QuotaExhausted, GeminiError))

    def test_remaining_batches_short_circuit(self):
        """Once the quota is gone, later batches must not each wait out four
        60-second backoffs -- that is ~20 minutes against a 25-minute timeout."""
        from evaluator import GeminiEvaluator, QuotaExhausted

        evaluator = GeminiEvaluator()
        calls = {"n": 0}

        def boom(batch, cv_text):
            calls["n"] += 1
            raise QuotaExhausted("HTTP 429: quota exceeded")

        evaluator.evaluate_batch = boom  # type: ignore[assignment]
        evaluator.batch_size = 2
        evaluator.concurrency = 1

        jobs = [
            JobPost(source="test", title=f"Engineer {i}", company="Acme",
                    location="Dubai", url=f"https://example.com/{i}")
            for i in range(10)
        ]
        results = evaluator.evaluate(jobs, "cv text")

        self.assertEqual(len(results), 10, "every posting must still get a verdict")
        self.assertTrue(all(r.error == "quota_exhausted" for r in results))
        self.assertTrue(evaluator.quota_exhausted)
        self.assertEqual(calls["n"], 1,
                         "only the FIRST batch should have hit the API")


class TestSourceHealthDigest(unittest.TestCase):
    """The digest exists to PROVE every source ran, so it must never drop one.

    CallMeBot carries the message in a query string with a hard URL ceiling.
    Nine sources at two lines each exceeds one message, so the digest splits
    across parts rather than truncating -- a truncated audit would quietly
    misreport a source as absent.
    """

    def setUp(self):
        from notifier import MAX_TEXT_CHARS, MAX_URL_LENGTH, WhatsAppNotifier
        from pipeline import RunReport

        self.N = WhatsAppNotifier
        self.RunReport = RunReport
        self.max_chars = MAX_TEXT_CHARS
        self.max_url = MAX_URL_LENGTH

    def _report(self, sources, **kw):
        r = self.RunReport()
        r.sources = sources
        for k, v in kw.items():
            setattr(r, k, v)
        return r

    @staticmethod
    def _src(name, count=10, ok=True, title="IT Support Engineer",
             company="Acme", error=""):
        return {"name": name, "ok": ok, "count": count, "error": error,
                "sample": {"title": title, "company": company} if title else {}}

    def _within_limits(self, parts):
        from urllib.parse import quote

        for p in parts:
            self.assertLessEqual(len(p), self.max_chars, "message text over budget")
            url = ("https://api.callmebot.com/whatsapp.php"
                   f"?phone=%2B201234567890&apikey=TESTKEY&text={quote(p)}")
            self.assertLessEqual(len(url), self.max_url, "encoded URL over budget")

    def test_every_source_appears_even_when_split(self):
        names = ["linkedin", "tanqeeb", "talent", "telegram", "telegram_user",
                 "job_apis", "search_proxy", "rss", "facebook"]
        sources = [self._src(n, count=500) for n in names]
        parts = self.N.format_source_digest(self._report(sources))
        rendered = sum(p.count("🔹") + p.count("🔻") for p in parts)
        self.assertEqual(rendered, len(names),
                         "a source was dropped to make the message fit")
        self._within_limits(parts)

    def test_pathological_lengths_still_fit(self):
        sources = [self._src(f"src{i}", title="X" * 400, company="Y" * 400)
                   for i in range(9)]
        parts = self.N.format_source_digest(self._report(sources))
        self._within_limits(parts)
        self.assertEqual(sum(p.count("🔹") for p in parts), 9)

    def test_arabic_content_fits_the_url_budget(self):
        """Arabic is 2 bytes/char and ~9 chars once percent-encoded."""
        sources = [
            self._src(f"tanqeeb{i}", title="اخصائي دعم فني وشبكات",
                      company="شركة النور للتكنولوجيا والاتصالات")
            for i in range(8)
        ]
        parts = self.N.format_source_digest(self._report(sources))
        self._within_limits(parts)

    def test_failed_source_is_marked_and_listed_first(self):
        sources = [self._src("linkedin", count=300),
                   self._src("rss", ok=False, count=0, error="HTTP 429")]
        parts = self.N.format_source_digest(self._report(sources))
        blob = "\n".join(parts)
        self.assertIn("🔻", blob, "a failed source was not flagged")
        self.assertIn("FAILED", blob)
        self.assertIn("HTTP 429", blob)
        self.assertLess(blob.index("🔻"), blob.index("🔹"),
                        "failures must surface above healthy sources")

    def test_empty_source_says_so_rather_than_looking_broken(self):
        parts = self.N.format_source_digest(
            self._report([self._src("facebook", count=0, title="")])
        )
        self.assertIn("nothing new", "\n".join(parts))

    def test_totals_are_reported(self):
        sources = [self._src("a", count=100), self._src("b", count=250)]
        parts = self.N.format_source_digest(
            self._report(sources, evaluated=40, matched=2, alerts_sent=2)
        )
        blob = "\n".join(parts)
        self.assertIn("350", blob, "total job count missing")
        self.assertIn("2/2 platforms", blob)
        self.assertIn("matched 2", blob)

    def test_no_sources_does_not_crash(self):
        parts = self.N.format_source_digest(self._report([]))
        self.assertTrue(parts)
        self._within_limits(parts)

    def test_parts_are_numbered_only_when_split(self):
        single = self.N.format_source_digest(self._report([self._src("a")]))
        self.assertEqual(len(single), 1)
        self.assertNotIn("part 1/", single[0])

        many = self.N.format_source_digest(
            self._report([self._src(f"s{i}", title="T" * 55) for i in range(12)])
        )
        self.assertGreater(len(many), 1)
        self.assertIn(f"part 1/{len(many)}", many[0])

    def test_unknown_scraper_still_gets_a_readable_label(self):
        parts = self.N.format_source_digest(
            self._report([self._src("some_new_board", count=5)])
        )
        self.assertIn("Some New Board", "\n".join(parts))

    def test_sample_prefers_a_posting_that_names_a_company(self):
        from datetime import timedelta

        from pipeline import _source_sample
        from scrapers.base import ScrapeResult

        older_named = JobPost(source="t", title="Named Role", company="Acme",
                              location="Dubai", url="https://e.com/1",
                              posted_at=utc_now() - timedelta(days=3))
        newer_anon = JobPost(source="t", title="Anon Role", company="",
                             location="Dubai", url="https://e.com/2",
                             posted_at=utc_now())
        sample = _source_sample(ScrapeResult("t", [newer_anon, older_named]))
        self.assertEqual(sample["company"], "Acme",
                         "a sample without a company is weak evidence")

    def test_sample_of_empty_source_is_empty(self):
        from pipeline import _source_sample
        from scrapers.base import ScrapeResult

        self.assertEqual(_source_sample(ScrapeResult("t", [])), {})


    def test_arabic_job_alert_fits_the_encoded_url(self):
        """Regression: Arabic alerts were silently dropped by CallMeBot.

        The old budget counted characters, so a 509-char Arabic alert passed
        the 900-char cap while being 1,985 URL characters -- over the limit.
        The fallback then computed a character `keep` larger than the message,
        trimmed nothing, and the send was discarded with no error.
        """
        from urllib.parse import quote

        from models import Evaluation
        from notifier import MAX_URL_LENGTH, WhatsAppNotifier

        ev = Evaluation(
            fingerprint="x",
            company_name="شركة النور للتكنولوجيا والاتصالات المتقدمة",
            role_title="اخصائي دعم فني وشبكات وسنترالات",
            location="القاهرة، جمهورية مصر العربية",
            match_score=88, source_platform="tanqeeb:egypt",
            direct_link="https://egypt.tanqeeb.com/jobs-in-middle-east/all/jobs/021171884.html",
            why_matched="الوظيفة تتطلب خبرة في السنترالات وأنظمة SIP و Issabel " * 4,
            skill_gaps=["شهادة سيسكو", "خبرة اضافية في Asterisk"],
        )
        msg = WhatsAppNotifier(None).format_alert(ev)
        url = ("https://api.callmebot.com/whatsapp.php"
               f"?phone=%2B201234567890&apikey=TESTKEY&text={quote(msg)}")
        self.assertLessEqual(len(url), MAX_URL_LENGTH,
                             "Arabic alert would be dropped by CallMeBot")
        self.assertIn("tanqeeb.com", msg, "the link must survive shrinking")
        self.assertIn("88%", msg)

    def test_send_raw_guard_bounds_any_message(self):
        """Even a caller that ignores the budget must not produce a dead URL."""
        from urllib.parse import quote

        from notifier import MAX_URL_LENGTH, WhatsAppNotifier

        n = WhatsAppNotifier(None)
        n.dry_run = False
        captured = {}

        def fake_get(url, **kw):
            captured["url"] = url
            raise RuntimeError("stop before the network")

        import http_client

        original = http_client.get
        http_client.get = fake_get
        try:
            n.send_raw("مطلوب مهندس دعم فني " * 200)
        finally:
            http_client.get = original
        self.assertLessEqual(len(captured["url"]), MAX_URL_LENGTH)


if __name__ == "__main__":
    unittest.main(verbosity=2)
