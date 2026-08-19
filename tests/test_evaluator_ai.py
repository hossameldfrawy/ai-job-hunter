"""
The Gemini layer: the prompt it builds, the JSON it accepts, and every way the
API refuses.

Three things make this worth testing hard even though the model itself is not
under test:

  1. THE PROMPT IS THE PRODUCT. The bilingual contract -- English reasoning for
     the Telegram card, deliberately short Arabic for the WhatsApp card -- lives
     in the system instruction and the response schema. If a refactor drops the
     Arabic fields, the WhatsApp alert silently loses half its content and every
     test that only checks `match_score` still passes.

  2. FAILURE MUST NEVER LOSE THE RUN. A model that is overloaded, retired or
     rate-limited has to degrade to "this batch scored 0 with a reason", not to
     an exception that discards fifteen other batches.

  3. AN EXHAUSTED QUOTA IS NOT A RATE LIMIT. A transient 429 clears inside its
     retryDelay; a spent daily allowance does not clear for hours. Retrying into
     the second one burns the whole run in backoff, which is why they are
     different exception types with different handling.

Every HTTP call is replaced. `time.sleep` is replaced too, so a four-retry
backoff test costs microseconds instead of half a minute.

Run:  python -m pytest tests/test_evaluator_ai.py -v
"""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import evaluator as evaluator_mod                                # noqa: E402
import http_client                                               # noqa: E402
from config import settings                                      # noqa: E402
from evaluator import (                                          # noqa: E402
    PRIMARY_MODEL, GeminiError, GeminiEvaluator, QuotaExhausted,
    RESPONSE_SCHEMA, RateLimiter, SYSTEM_INSTRUCTION, _retry_delay_from,
)
from models import Evaluation, JobPost                           # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Replays a scripted list of responses and records what was sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.requests.append({"url": url, "body": json})
        response = self.responses.pop(0) if self.responses else FakeResponse()
        if isinstance(response, Exception):
            raise response
        return response


@contextmanager
def scripted(responses, pace: float = 0.0):
    """Replace the HTTP session and neutralise every sleep.

    `pace` sets the rate limiter's floor and defaults to ZERO. The limiter
    sleeps through the same `time.sleep` these tests record, so leaving it at
    its production 3.5s would fold pacing delays into the backoff assertions
    and make "was this retried correctly?" unanswerable. The limiter's own
    behaviour is tested directly in TestRateLimiter instead.
    """
    session = FakeSession(responses)
    original_session = http_client.session
    original_sleep = evaluator_mod.time.sleep
    original_pace = evaluator_mod.RateLimiter._sleep
    original_gemini = dict(settings.raw.get("gemini", {}) or {})
    paced: list[float] = []
    slept: list[float] = []
    http_client.session = lambda: session
    evaluator_mod.time.sleep = slept.append
    # The limiter has its own sleep seam, so pacing can be neutralised without
    # hiding the backoff these tests are actually about.
    evaluator_mod.RateLimiter._sleep = staticmethod(paced.append)
    settings.raw["gemini"] = dict(original_gemini,
                                  min_request_interval_seconds=pace,
                                  # Headroom to widen into: a ceiling equal to
                                  # the floor makes penalise() a no-op.
                                  max_request_interval_seconds=max(pace * 10,
                                                                   30.0))
    try:
        yield session, slept
    finally:
        http_client.session = original_session
        evaluator_mod.time.sleep = original_sleep
        evaluator_mod.RateLimiter._sleep = original_pace
        settings.raw["gemini"] = original_gemini


def _ok(evaluations):
    """A well-formed Gemini success envelope."""
    return FakeResponse(200, {
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": json.dumps(
                {"evaluations": evaluations}, ensure_ascii=False
            )}]},
        }],
        "usageMetadata": {"totalTokenCount": 1234},
    })


def _verdict(index=0, **overrides):
    verdict = {
        "job_index": index,
        "is_real_job": True,
        "company_name": "Etisalat",
        "role_title": "VoIP Engineer",
        "location": "Dubai, UAE",
        "salary": "18,000 AED / month",
        "match_score": 88,
        "why_matched_en": "Requires Asterisk/FreePBX administration and SIP "
                          "trunk troubleshooting, which maps to the candidate's "
                          "Issabel PBX work.",
        "gaps_en": "Requires 5+ years, whereas the candidate has three",
        "arabic_summary": "وظيفة مهندس VoIP في دبي",
        "why_matched_ar": "خبرته في Asterisk و SIP مطابقة",
        "gaps_ar": "يطلبون خبرة اطول",
    }
    verdict.update(overrides)
    return verdict


def _job(index=1):
    return JobPost(
        source="tanqeeb:uae",
        title=f"VoIP Engineer {index}",
        company="Etisalat",
        location="Dubai",
        url=f"https://uae.tanqeeb.com/jobs/{index}.html",
        description="Administer Asterisk and SIP trunks.",
    )


CV = "VoIP engineer with SIP, Asterisk and Issabel PBX experience."


# ---------------------------------------------------------------------------
class TestPromptConstruction(unittest.TestCase):
    def test_the_request_carries_the_cv_and_every_job(self):
        with scripted([_ok([_verdict(0), _verdict(1)])]) as (session, _):
            GeminiEvaluator().evaluate_batch([_job(1), _job(2)], CV)
        prompt = session.requests[0]["body"]["contents"][0]["parts"][0]["text"]
        self.assertIn(CV, prompt)
        self.assertIn("VoIP Engineer 1", prompt)
        self.assertIn("VoIP Engineer 2", prompt)
        self.assertIn("### JOB 0", prompt)
        self.assertIn("### JOB 1", prompt)

    def test_the_model_is_told_how_many_verdicts_to_return(self):
        """Gemini silently skips items otherwise, and a skipped job scores 0."""
        with scripted([_ok([_verdict(0)])]) as (session, _):
            GeminiEvaluator().evaluate_batch([_job(1)], CV)
        prompt = session.requests[0]["body"]["contents"][0]["parts"][0]["text"]
        self.assertIn("Return exactly 1 evaluation", prompt)

    def test_json_mode_is_enforced_at_decode_time_not_by_prompting(self):
        with scripted([_ok([_verdict(0)])]) as (session, _):
            GeminiEvaluator().evaluate_batch([_job(1)], CV)
        config = session.requests[0]["body"]["generationConfig"]
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertEqual(config["responseSchema"], RESPONSE_SCHEMA)

    def test_safety_settings_do_not_silently_drop_job_posts(self):
        """Salary and nationality wording trips the default thresholds."""
        with scripted([_ok([_verdict(0)])]) as (session, _):
            GeminiEvaluator().evaluate_batch([_job(1)], CV)
        settings_sent = session.requests[0]["body"]["safetySettings"]
        self.assertTrue(settings_sent)
        self.assertTrue(all(s["threshold"] == "BLOCK_ONLY_HIGH"
                            for s in settings_sent))

    def test_the_schema_demands_both_languages(self):
        """The two cards are NOT translations of each other; both are required."""
        props = RESPONSE_SCHEMA["properties"]["evaluations"]["items"]
        for field in ("why_matched_en", "gaps_en", "arabic_summary",
                      "why_matched_ar", "gaps_ar"):
            with self.subTest(field=field):
                self.assertIn(field, props["properties"])
                self.assertIn(field, props["required"])

    def test_the_arabic_fields_document_their_character_budget(self):
        """They render on a card with a hard percent-encoded size limit."""
        props = RESPONSE_SCHEMA["properties"]["evaluations"]["items"]["properties"]
        for field in ("arabic_summary", "why_matched_ar", "gaps_ar"):
            with self.subTest(field=field):
                self.assertIn("characters", props[field]["description"])

    def test_the_rubric_bans_an_invented_salary(self):
        self.assertIn("Never estimate a market rate", SYSTEM_INSTRUCTION)
        salary = (RESPONSE_SCHEMA["properties"]["evaluations"]["items"]
                  ["properties"]["salary"]["description"])
        self.assertIn("EMPTY STRING", salary)

    def test_the_rubric_caps_senior_roles_for_an_early_career_engineer(self):
        self.assertIn("must not\n    exceed 60", SYSTEM_INSTRUCTION)

    def test_listing_pages_are_defined_as_not_real_jobs(self):
        self.assertIn("is_real_job=false", SYSTEM_INSTRUCTION)


# ---------------------------------------------------------------------------
class TestVerdictParsing(unittest.TestCase):
    def test_a_full_verdict_becomes_an_evaluation(self):
        with scripted([_ok([_verdict(0)])]):
            [ev] = GeminiEvaluator().evaluate_batch([_job(1)], CV)
        self.assertIsInstance(ev, Evaluation)
        self.assertEqual(ev.match_score, 88)
        self.assertEqual(ev.company_name, "Etisalat")
        self.assertEqual(ev.salary, "18,000 AED / month")
        self.assertIn("Asterisk", ev.why_matched)
        self.assertIn("دبي", ev.arabic_summary)
        self.assertIn("Asterisk", ev.why_matched_ar)
        self.assertTrue(ev.gaps_ar)

    def test_the_link_and_source_come_from_the_job_never_the_model(self):
        """A hallucinated link costs the user a dead end -- the one field where
        the model is not trusted at all."""
        with scripted([_ok([_verdict(0, direct_link="https://evil.example/x",
                                     source_platform="linkedin")])]):
            [ev] = GeminiEvaluator().evaluate_batch([_job(7)], CV)
        self.assertEqual(ev.direct_link, "https://uae.tanqeeb.com/jobs/7.html")
        self.assertEqual(ev.source_platform, "tanqeeb:uae")

    def test_gap_analysis_splits_a_list_but_keeps_an_explanatory_clause(self):
        """"Requires 2+ years, whereas the candidate has one" is ONE gap.

        Splitting naively on the comma produces a fragment that renders as its
        own bullet on the card and reads as a bug.
        """
        gaps = "No CCNA, Kubernetes exposure, whereas the candidate has three years"
        with scripted([_ok([_verdict(0, gaps_en=gaps)])]):
            [ev] = GeminiEvaluator().evaluate_batch([_job(1)], CV)
        self.assertEqual(len(ev.skill_gaps), 2, ev.skill_gaps)
        self.assertEqual(ev.skill_gaps[0], "No CCNA")
        self.assertIn("whereas", ev.skill_gaps[1])

    def test_a_lower_case_tail_is_folded_into_the_gap_it_belongs_to(self):
        with scripted([_ok([_verdict(0, gaps_en="No CCNA, requires 5+ years")])]):
            [ev] = GeminiEvaluator().evaluate_batch([_job(1)], CV)
        self.assertEqual(ev.skill_gaps, ["No CCNA, requires 5+ years"])

    def test_a_not_real_job_is_forced_to_zero(self):
        with scripted([_ok([_verdict(0, is_real_job=False, match_score=91)])]):
            [ev] = GeminiEvaluator().evaluate_batch([_job(1)], CV)
        self.assertEqual(ev.match_score, 0)
        self.assertEqual(ev.error, "not_a_real_vacancy")

    def test_a_skipped_job_index_scores_zero_rather_than_vanishing(self):
        """Silently dropping it would leave the posting marked seen and never
        evaluated -- invisible, and permanent."""
        with scripted([_ok([_verdict(0)])]):
            results = GeminiEvaluator().evaluate_batch([_job(1), _job(2)], CV)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[1].match_score, 0)
        self.assertEqual(results[1].error, "missing_from_response")

    def test_scores_are_clamped_to_the_range(self):
        for raw, expected in ((150, 100), (-5, 0), ("77", 77), ("abc", 0),
                              (None, 0), (88.7, 88)):
            with self.subTest(raw=raw):
                with scripted([_ok([_verdict(0, match_score=raw)])]):
                    [ev] = GeminiEvaluator().evaluate_batch([_job(1)], CV)
                self.assertEqual(ev.match_score, expected)

    def test_partial_json_is_salvaged_rather_than_discarded(self):
        """A truncated response still contains verdicts worth keeping."""
        payload = {"candidates": [{
            "finishReason": "MAX_TOKENS",
            "content": {"parts": [{"text":
                "here you go: " + json.dumps({"evaluations": [_verdict(0)]})}]},
        }]}
        with scripted([FakeResponse(200, payload)]):
            [ev] = GeminiEvaluator().evaluate_batch([_job(1)], CV)
        self.assertEqual(ev.match_score, 88)

    def test_a_truncated_response_names_the_token_budget_as_the_cause(self):
        """Seen live: an Arabic cover letter blew a 3072-token budget and the
        error read "unparseable JSON" -- which points at the parser rather than
        at the setting that actually needs changing."""
        payload = {"candidates": [{
            "finishReason": "MAX_TOKENS",
            "content": {"parts": [{"text": '{"cover_letter": "فريق التوظيف في'}]},
        }]}
        with scripted([FakeResponse(200, payload)]):
            with self.assertRaises(GeminiError) as ctx:
                GeminiEvaluator().evaluate_batch([_job(1)], CV)
        message = str(ctx.exception)
        self.assertIn("MAX_TOKENS", message)
        self.assertIn("maxOutputTokens", message)
        self.assertIn("Arabic", message)

    def test_the_drafting_call_has_headroom_for_arabic(self):
        """Arabic costs ~3x the tokens of the equivalent English, and the Gulf
        boards this bot reads are mostly Arabic."""
        from auto_apply.engine import draft_answers

        seen = {}

        def capture(self, body):
            seen["config"] = body["generationConfig"]
            return ({"candidates": [{"content": {"parts": [{"text": json.dumps({
                "cover_letter": "x", "answers": [], "salary_expectation": "",
            })}]}}]}, "fake-model")

        import evaluator as evaluator_mod

        original = evaluator_mod.GeminiEvaluator._generate
        evaluator_mod.GeminiEvaluator._generate = capture
        try:
            draft_answers(Evaluation(fingerprint="fp"), "a job", [])
        finally:
            evaluator_mod.GeminiEvaluator._generate = original
        self.assertGreaterEqual(seen["config"]["maxOutputTokens"], 8192)

    def test_unparseable_output_raises_a_gemini_error(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "nope"}]}}]}
        with scripted([FakeResponse(200, payload)]):
            with self.assertRaises(GeminiError):
                GeminiEvaluator().evaluate_batch([_job(1)], CV)

    def test_a_blocked_prompt_names_the_block_reason(self):
        payload = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
        with scripted([FakeResponse(200, payload)]):
            with self.assertRaises(GeminiError) as ctx:
                GeminiEvaluator().evaluate_batch([_job(1)], CV)
        self.assertIn("SAFETY", str(ctx.exception))

    def test_an_empty_batch_costs_nothing(self):
        with scripted([]) as (session, _):
            self.assertEqual(GeminiEvaluator().evaluate_batch([], CV), [])
        self.assertEqual(session.requests, [])

    def test_token_usage_is_accumulated(self):
        evaluator = GeminiEvaluator()
        with scripted([_ok([_verdict(0)])]):
            evaluator.evaluate_batch([_job(1)], CV)
        self.assertEqual(evaluator.tokens_used, 1234)
        self.assertEqual(evaluator.calls_made, 1)


# ---------------------------------------------------------------------------
class TestRetriesAndModelChain(unittest.TestCase):
    def test_a_transient_429_is_retried_and_then_succeeds(self):
        responses = [FakeResponse(429, {}), _ok([_verdict(0)])]
        with scripted(responses) as (session, slept):
            [ev] = GeminiEvaluator().evaluate_batch([_job(1)], CV)
        self.assertEqual(ev.match_score, 88)
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(len(slept), 1, "no backoff was applied")

    def test_googles_own_retry_delay_is_honoured_over_our_guess(self):
        payload = {"error": {"details": [{"retryDelay": "17.5s"}]}}
        with scripted([FakeResponse(429, payload), _ok([_verdict(0)])]) as (_, slept):
            GeminiEvaluator().evaluate_batch([_job(1)], CV)
        self.assertEqual(slept, [17.5])

    def test_the_retry_delay_parser_handles_every_shape(self):
        self.assertEqual(
            _retry_delay_from({"error": {"details": [{"retryDelay": "3s"}]}}), 3.0
        )
        self.assertIsNone(_retry_delay_from({}))
        self.assertIsNone(_retry_delay_from({"error": {"details": [{}]}}))
        self.assertIsNone(_retry_delay_from({"error": {"details": "junk"}}))

    def test_a_backoff_is_never_longer_than_a_minute(self):
        """A capped wait keeps one slow model from eating the whole run."""
        payload = {"error": {"details": [{"retryDelay": "3600s"}]}}
        with scripted([FakeResponse(503, payload), _ok([_verdict(0)])]) as (_, slept):
            GeminiEvaluator().evaluate_batch([_job(1)], CV)
        self.assertLessEqual(slept[0], 60)

    def test_server_errors_are_retried(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                with scripted([FakeResponse(status, {}), _ok([_verdict(0)])]):
                    [ev] = GeminiEvaluator().evaluate_batch([_job(1)], CV)
                self.assertEqual(ev.match_score, 88)

    def test_a_permanent_error_falls_through_to_the_next_model(self):
        """404/400 mean THIS model is gone; burning retries on it is waste."""
        evaluator = GeminiEvaluator()
        evaluator.models = ["gemini-retired", "gemini-2.5-flash"]
        with scripted([FakeResponse(404, {}, "model not found"),
                       _ok([_verdict(0)])]) as (session, slept):
            [ev] = evaluator.evaluate_batch([_job(1)], CV)
        self.assertEqual(ev.match_score, 88)
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(slept, [], "retries were burned on a dead model")
        self.assertIn("gemini-2.5-flash", session.requests[1]["url"])

    def test_the_working_model_becomes_sticky_for_the_rest_of_the_run(self):
        evaluator = GeminiEvaluator()
        evaluator.models = ["gemini-retired", "gemini-2.5-flash"]
        with scripted([FakeResponse(404, {}), _ok([_verdict(0)]),
                       _ok([_verdict(0)])]) as (session, _):
            evaluator.evaluate_batch([_job(1)], CV)
            evaluator.evaluate_batch([_job(2)], CV)
        self.assertIn("gemini-2.5-flash", session.requests[2]["url"])
        self.assertEqual(len(session.requests), 3,
                         "the dead model was tried again")

    def test_every_model_failing_names_all_of_them(self):
        evaluator = GeminiEvaluator()
        evaluator.models = ["a", "b"]
        with scripted([FakeResponse(404, {}), FakeResponse(404, {})]):
            with self.assertRaises(GeminiError) as ctx:
                evaluator.evaluate_batch([_job(1)], CV)
        self.assertIn("a:", str(ctx.exception))
        self.assertIn("b:", str(ctx.exception))

    def test_a_network_exception_is_retried_not_propagated(self):
        import requests

        with scripted([requests.ConnectionError("dns"), _ok([_verdict(0)])]):
            [ev] = GeminiEvaluator().evaluate_batch([_job(1)], CV)
        self.assertEqual(ev.match_score, 88)


# ---------------------------------------------------------------------------
class TestQuotaExhaustion(unittest.TestCase):
    def test_a_spent_quota_is_its_own_exception_type(self):
        """It is NOT a transient rate limit: it does not clear for hours."""
        self.assertTrue(issubclass(QuotaExhausted, GeminiError))

    def test_the_quota_signal_survives_the_model_chain(self):
        """Regression: `QuotaExhausted` IS a `GeminiError`, so the chain's
        catch-all swallowed it and re-raised a plain error -- which made the
        short-circuit in `evaluate()` unreachable and left every remaining
        batch waiting out four backoffs against a quota that would not clear
        for hours."""
        evaluator = GeminiEvaluator()
        evaluator.models = ["only"]
        with scripted([FakeResponse(429, {})] * evaluator.max_retries):
            with self.assertRaises(QuotaExhausted):
                evaluator.evaluate_batch([_job(1)], CV)

    def test_a_quota_blocked_model_covered_by_another_is_not_an_outage(self):
        """One model out of quota while a second answers is a normal run."""
        evaluator = GeminiEvaluator()
        evaluator.models = ["spent", "working"]
        responses = [FakeResponse(429, {})] * evaluator.max_retries
        responses.append(_ok([_verdict(0)]))
        with scripted(responses):
            [ev] = evaluator.evaluate_batch([_job(1)], CV)
        self.assertEqual(ev.match_score, 88)
        self.assertFalse(evaluator.quota_exhausted)

    def test_a_non_quota_chain_failure_stays_a_plain_gemini_error(self):
        evaluator = GeminiEvaluator()
        evaluator.models = ["gone"]
        with scripted([FakeResponse(404, {}, "model not found")]):
            with self.assertRaises(GeminiError) as ctx:
                evaluator.evaluate_batch([_job(1)], CV)
        self.assertNotIsInstance(ctx.exception, QuotaExhausted)

    def test_the_remaining_batches_short_circuit_instead_of_waiting(self):
        """Otherwise every one of fifteen batches waits out four backoffs."""
        evaluator = GeminiEvaluator()
        evaluator.models = ["only"]
        evaluator.batch_size = 1
        evaluator.concurrency = 1
        jobs = [_job(i) for i in range(1, 6)]
        with scripted([FakeResponse(429, {})] * evaluator.max_retries) as (s, _):
            results = evaluator.evaluate(jobs, CV)
        self.assertEqual(len(results), 5, "postings were lost, not just failed")
        self.assertTrue(all(r.error == "quota_exhausted" for r in results))
        self.assertTrue(evaluator.quota_exhausted)
        self.assertEqual(
            len(s.requests), evaluator.max_retries,
            "the later batches retried into an exhausted quota",
        )

    def test_a_failed_batch_keeps_the_posting_identifiable(self):
        """The pipeline un-sees these so a later run retries them; that only
        works if the fingerprint survives."""
        evaluator = GeminiEvaluator()
        evaluator.models = ["only"]
        job = _job(1)
        with scripted([FakeResponse(429, {})] * evaluator.max_retries):
            results = evaluator.evaluate([job], CV)
        self.assertEqual(results[0].fingerprint, job.fingerprint)
        self.assertEqual(results[0].match_score, 0)


# ---------------------------------------------------------------------------
class TestEvaluateNeverRaises(unittest.TestCase):
    """`evaluate()` is the pipeline's boundary; nothing may escape it."""

    def test_a_parse_failure_degrades_to_scored_zero(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "garbage"}]}}]}
        with scripted([FakeResponse(200, payload)] * 3):
            results = GeminiEvaluator().evaluate([_job(1)], CV)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].match_score, 0)
        self.assertTrue(results[0].error)

    def test_an_unexpected_exception_is_contained(self):
        with scripted([RuntimeError("something absurd")] * 8):
            results = GeminiEvaluator().evaluate([_job(1)], CV)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].match_score, 0)

    def test_batches_are_delivered_incrementally(self):
        """A crash halfway must not discard the work already paid for."""
        evaluator = GeminiEvaluator()
        evaluator.batch_size = 1
        evaluator.concurrency = 1
        seen: list[int] = []
        with scripted([_ok([_verdict(0)]), _ok([_verdict(0)])]):
            evaluator.evaluate([_job(1), _job(2)], CV, on_batch=lambda b: seen.append(len(b)))
        self.assertEqual(seen, [1, 1])

    def test_a_failing_callback_does_not_lose_the_results(self):
        evaluator = GeminiEvaluator()
        evaluator.batch_size = 1
        with scripted([_ok([_verdict(0)])]):
            results = evaluator.evaluate(
                [_job(1)], CV,
                on_batch=lambda b: (_ for _ in ()).throw(IOError("disk full")),
            )
        self.assertEqual(len(results), 1)

    def test_no_jobs_means_no_calls(self):
        with scripted([]) as (session, _):
            self.assertEqual(GeminiEvaluator().evaluate([], CV), [])
        self.assertEqual(session.requests, [])

    def test_batching_cuts_the_request_count(self):
        """Batching -- not prompt trimming -- is what keeps the free tier alive."""
        evaluator = GeminiEvaluator()
        evaluator.batch_size = 8
        evaluator.concurrency = 1
        jobs = [_job(i) for i in range(1, 17)]
        with scripted([_ok([_verdict(i) for i in range(8)])] * 2) as (s, _):
            results = evaluator.evaluate(jobs, CV)
        self.assertEqual(len(results), 16)
        self.assertEqual(len(s.requests), 2, "16 jobs should cost 2 calls")


# ---------------------------------------------------------------------------
class TestSelftest(unittest.TestCase):
    def test_selftest_reports_success(self):
        payload = {"candidates": [{"content": {"parts": [{"text": '{"ok":true}'}]}}]}
        with scripted([FakeResponse(200, payload)]):
            ok, detail = GeminiEvaluator().selftest()
        self.assertTrue(ok, detail)

    def test_selftest_reports_failure_without_raising(self):
        with scripted([FakeResponse(403, {}, "API key not valid")] * 4):
            ok, detail = GeminiEvaluator().selftest()
        self.assertFalse(ok)
        self.assertTrue(detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
class TestPrimaryModelEnforcement(unittest.TestCase):
    """One model, deliberately. The rubric is calibrated against it."""

    def test_the_primary_is_gemini_3_5_flash(self):
        self.assertEqual(PRIMARY_MODEL, "gemini-3.5-flash")

    def test_it_leads_the_chain_as_configured(self):
        with scripted([]):
            self.assertEqual(GeminiEvaluator().models[0], PRIMARY_MODEL)

    def test_a_config_that_demotes_it_is_corrected_and_warned_about(self):
        """A stale config must not quietly move the whole pipeline onto a
        different model's scoring behaviour."""
        original = dict(settings.raw.get("gemini", {}) or {})
        settings.raw["gemini"] = dict(
            original, model_chain=["gemini-2.5-flash", PRIMARY_MODEL])
        try:
            with self.assertLogs("evaluator", level="WARNING") as captured:
                evaluator = GeminiEvaluator()
        finally:
            settings.raw["gemini"] = original
        self.assertEqual(evaluator.models[0], PRIMARY_MODEL)
        self.assertIn(PRIMARY_MODEL, "\n".join(captured.output))

    def test_the_primary_is_never_duplicated_when_promoted(self):
        original = dict(settings.raw.get("gemini", {}) or {})
        settings.raw["gemini"] = dict(
            original, model_chain=["gemini-2.5-flash", PRIMARY_MODEL])
        try:
            models = GeminiEvaluator().models
        finally:
            settings.raw["gemini"] = original
        self.assertEqual(models.count(PRIMARY_MODEL), 1)

    def test_an_empty_chain_falls_back_to_the_primary_not_an_old_model(self):
        original = dict(settings.raw.get("gemini", {}) or {})
        settings.raw["gemini"] = dict(original, model_chain=[])
        try:
            self.assertEqual(GeminiEvaluator().models, [PRIMARY_MODEL])
        finally:
            settings.raw["gemini"] = original

    def test_the_primary_is_used_when_it_answers(self):
        with scripted([_ok([_verdict(0)])]) as (session, _):
            GeminiEvaluator().evaluate_batch([_job(1)], CV)
        self.assertIn(PRIMARY_MODEL, session.requests[0]["url"])

    def test_a_transient_429_does_NOT_move_off_the_primary(self):
        """Only a HARD failure may change model. A rate limit is not one --
        switching on it would silently rescore the run on a different rubric."""
        with scripted([FakeResponse(429, {}), _ok([_verdict(0)])]) as (s, _):
            evaluator = GeminiEvaluator()
            evaluator.evaluate_batch([_job(1)], CV)
        self.assertTrue(all(PRIMARY_MODEL in r["url"] for r in s.requests),
                        [r["url"] for r in s.requests])
        self.assertEqual(evaluator._preferred, PRIMARY_MODEL)

    def test_leaving_the_primary_is_logged_as_an_error_not_a_shrug(self):
        """It used to be one WARNING among many, so a run that finished on a
        different model looked identical to one that did not."""
        with scripted([FakeResponse(404, {}, "gone"), _ok([_verdict(0)])]):
            with self.assertLogs("evaluator", level="ERROR") as captured:
                GeminiEvaluator().evaluate_batch([_job(1)], CV)
        joined = "\n".join(captured.output)
        self.assertIn("PRIMARY MODEL FAILED HARD", joined)
        self.assertIn(PRIMARY_MODEL, joined)


class TestRateLimiter(unittest.TestCase):
    """Prevention, not cure: the 429 should never happen in the first place."""

    def _limiter(self, **kwargs):
        limiter = RateLimiter(**kwargs)
        limiter.slept = []
        limiter._sleep = limiter.slept.append
        return limiter

    def test_the_first_request_is_not_delayed(self):
        limiter = self._limiter(min_interval=3.5)
        self.assertEqual(limiter.acquire(), 0)
        self.assertEqual(limiter.slept, [])

    def test_subsequent_requests_are_spaced(self):
        limiter = self._limiter(min_interval=3.5)
        limiter.acquire()
        delay = limiter.acquire()
        self.assertAlmostEqual(delay, 3.5, delta=0.2)
        self.assertEqual(len(limiter.slept), 1)

    def test_a_429_widens_the_gap_for_every_later_request(self):
        """Otherwise the next batch walks into the same wall."""
        limiter = self._limiter(min_interval=4.0)
        self.assertAlmostEqual(limiter.penalise(), 6.0, delta=0.01)
        self.assertAlmostEqual(limiter.penalise(), 9.0, delta=0.01)
        self.assertEqual(limiter.throttle_events, 2)

    def test_the_gap_is_capped(self):
        limiter = self._limiter(min_interval=4.0, max_interval=10.0)
        for _ in range(20):
            limiter.penalise()
        self.assertLessEqual(limiter.interval, 10.0)

    def test_a_clean_streak_eases_the_pace_back_down(self):
        """One transient rate limit must not slow the whole rest of the run."""
        limiter = self._limiter(min_interval=4.0, recovery_after=3)
        limiter.penalise()
        widened = limiter.interval
        for _ in range(3):
            limiter.reward()
        self.assertLess(limiter.interval, widened)

    def test_it_never_eases_below_the_floor(self):
        limiter = self._limiter(min_interval=4.0, recovery_after=1)
        for _ in range(50):
            limiter.reward()
        self.assertAlmostEqual(limiter.interval, 4.0, delta=0.01)

    def test_concurrent_callers_are_spaced_from_each_other(self):
        """A per-thread limiter would let N threads each send politely at the
        same instant -- which is exactly the burst the quota counts."""
        import threading

        limiter = self._limiter(min_interval=2.0)
        delays = []
        lock = threading.Lock()

        def worker():
            delay = limiter.acquire()
            with lock:
                delays.append(delay)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # Four callers spaced 2s apart: 0, 2, 4, 6.
        self.assertAlmostEqual(max(delays), 6.0, delta=0.5)
        self.assertEqual(len(delays), 4)

    def test_a_zero_interval_disables_pacing_entirely(self):
        limiter = self._limiter(min_interval=0.0)
        for _ in range(5):
            limiter.acquire()
        self.assertEqual(limiter.slept, [])

    def test_the_snapshot_reports_what_pacing_cost(self):
        limiter = self._limiter(min_interval=1.0)
        limiter.acquire()
        limiter.acquire()
        limiter.penalise()
        snapshot = limiter.snapshot()
        self.assertGreater(snapshot["waited_seconds"], 0)
        self.assertEqual(snapshot["throttle_events"], 1.0)

    def test_the_evaluator_paces_every_request_including_retries(self):
        with scripted([FakeResponse(429, {}), _ok([_verdict(0)])],
                      pace=2.0) as (session, _):
            evaluator = GeminiEvaluator()
            paced = []
            evaluator.limiter._sleep = paced.append
            evaluator.evaluate_batch([_job(1)], CV)
        self.assertEqual(len(session.requests), 2)
        self.assertTrue(paced, "the retry was sent without pacing")

    def test_a_429_widens_the_evaluators_own_limiter(self):
        with scripted([FakeResponse(429, {}), _ok([_verdict(0)])],
                      pace=2.0) as (_session, _slept):
            evaluator = GeminiEvaluator()
            evaluator.limiter._sleep = lambda _d: None
            before = evaluator.limiter.interval
            evaluator.evaluate_batch([_job(1)], CV)
        self.assertGreater(evaluator.limiter.interval, before)


class TestQuotaCooldown(unittest.TestCase):
    """A spent daily allowance must be paid for ONCE, not once per draft.

    `draft_answers` builds a fresh evaluator for every application, so without
    a process-wide memory each one re-walks the chain from the top and pays the
    full retry ladder again. Measured live once the allowance was gone: eight
    minutes per draft, every draft, to reach the same conclusion.
    """

    def setUp(self):
        GeminiEvaluator.reset_quota_cooldowns()
        self.addCleanup(GeminiEvaluator.reset_quota_cooldowns)

    def _exhaust(self, evaluator):
        retries = [FakeResponse(429, {})] * evaluator.max_retries

        def responses(models):
            return retries * models

        return responses

    def test_a_quota_failure_benches_the_model(self):
        with scripted([FakeResponse(429, {})] * 4):
            evaluator = GeminiEvaluator()
            evaluator.models = ["only"]
            with self.assertRaises(QuotaExhausted):
                evaluator.evaluate_batch([_job(1)], CV)
        self.assertTrue(GeminiEvaluator._benched("only"))

    def test_a_benched_model_is_not_tried_again(self):
        """The whole point: the second draft must not repeat the ladder."""
        with scripted([FakeResponse(429, {})] * 4):
            first = GeminiEvaluator()
            first.models = ["spent", "working"]
            # `spent` burns its retries, then `working` has no response left
            # and returns the default 200 envelope.
            try:
                first.evaluate_batch([_job(1)], CV)
            except Exception:
                pass
        self.assertTrue(GeminiEvaluator._benched("spent"))

        with scripted([_ok([_verdict(0)])]) as (session, _):
            second = GeminiEvaluator()
            second.models = ["spent", "working"]
            second.evaluate_batch([_job(1)], CV)
        self.assertEqual(len(session.requests), 1, "the benched model was retried")
        self.assertIn("working", session.requests[0]["url"])

    def test_the_bench_is_shared_across_evaluator_instances(self):
        GeminiEvaluator._bench("shared-model")
        self.assertTrue(GeminiEvaluator()._benched("shared-model"))

    def test_an_expired_bench_lets_the_model_back_in(self):
        """A daemon must pick the model up again when the allowance resets."""
        GeminiEvaluator._bench("temporarily-out", seconds=-1)
        self.assertFalse(GeminiEvaluator._benched("temporarily-out"))

    def test_benching_everything_does_not_stop_the_run(self):
        """A stale cooldown must never be the reason no work happens at all."""
        GeminiEvaluator._bench("a")
        GeminiEvaluator._bench("b")
        with scripted([_ok([_verdict(0)])]) as (session, _):
            evaluator = GeminiEvaluator()
            evaluator.models = ["a", "b"]
            [ev] = evaluator.evaluate_batch([_job(1)], CV)
        self.assertEqual(ev.match_score, 88)
        self.assertTrue(session.requests, "nothing was attempted at all")

    def test_skipping_a_benched_model_is_logged(self):
        GeminiEvaluator._bench("spent")
        with scripted([_ok([_verdict(0)])]):
            with self.assertLogs("evaluator", level="INFO") as captured:
                evaluator = GeminiEvaluator()
                evaluator.models = ["spent", "working"]
                evaluator.evaluate_batch([_job(1)], CV)
        self.assertIn("out of quota", "\n".join(captured.output))

    def test_a_non_quota_failure_does_not_bench_anything(self):
        """A retired model is a different problem from a spent allowance, and
        benching on 404 would hide a config error behind a 15-minute silence."""
        with scripted([FakeResponse(404, {}, "gone"), _ok([_verdict(0)])]):
            evaluator = GeminiEvaluator()
            evaluator.models = ["retired", "working"]
            evaluator.evaluate_batch([_job(1)], CV)
        self.assertFalse(GeminiEvaluator._benched("retired"))

    def test_the_bench_message_says_how_long(self):
        with scripted([FakeResponse(429, {})] * 4):
            with self.assertLogs("evaluator", level="ERROR") as captured:
                evaluator = GeminiEvaluator()
                evaluator.models = [PRIMARY_MODEL]
                try:
                    evaluator.evaluate_batch([_job(1)], CV)
                except Exception:
                    pass
        self.assertIn("benched for", "\n".join(captured.output))
