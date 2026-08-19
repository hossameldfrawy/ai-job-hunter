"""
Gemini matching engine.

Takes raw postings plus the master CV and returns a strict, schema-validated
verdict for each one. Everything here is built around three hard requirements:

  1. STRICT JSON, ALWAYS. Gemini's `responseSchema` + `responseMimeType:
     application/json` are used so the model is constrained at decode time
     rather than trusted to format correctly. No regex JSON-scraping, no
     "the model returned prose" failure mode.

  2. QUOTA DISCIPLINE. Postings are evaluated in BATCHES (default 8 per call),
     which cuts request count ~8x. The free tier is measured in requests per
     minute, so batching -- not prompt trimming -- is what keeps this running
     24/7 for free.

  3. NEVER LOSE THE RUN. A model that is overloaded, rate-limited or retired
     falls through a chain of alternatives; only if every model in the chain
     fails does the batch surface as an error, and even then the other batches
     still complete.

The model is deliberately NOT trusted with `direct_link` or `source_platform`:
those are copied from the scraped record, because a link is the one field where
a hallucination costs the user a dead end.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable

import requests

import http_client
from config import settings
from models import Evaluation, JobPost

log = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Gemini uses an OpenAPI 3 subset: type names are UPPERCASE.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "evaluations": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "job_index": {
                        "type": "INTEGER",
                        "description": "The JOB number this verdict refers to.",
                    },
                    "is_real_job": {
                        "type": "BOOLEAN",
                        "description": (
                            "false if this is a board listing page, a search-"
                            "results page, a candidate CV/profile, an article, "
                            "or an advert rather than one specific vacancy."
                        ),
                    },
                    "company_name": {"type": "STRING"},
                    "role_title": {"type": "STRING"},
                    "location": {"type": "STRING"},
                    "salary": {
                        "type": "STRING",
                        "description": (
                            "Pay exactly as the posting states it, with currency "
                            "and period: '25,000-35,000 EGP / month'. If no figure "
                            "is given return an EMPTY STRING -- never guess a "
                            "market rate, because an invented salary is worse "
                            "than no salary."
                        ),
                    },
                    "match_score": {
                        "type": "INTEGER",
                        "description": "0-100 fit against the candidate's CV.",
                    },
                    "why_matched_en": {
                        "type": "STRING",
                        "description": (
                            "ENGLISH, one or two sentences naming the SPECIFIC "
                            "overlapping technologies. No generic praise."
                        ),
                    },
                    "gaps_en": {
                        "type": "STRING",
                        "description": (
                            "ENGLISH, comma-separated concrete requirements the CV "
                            "does not evidence. Empty string if none."
                        ),
                    },
                    "arabic_summary": {
                        "type": "STRING",
                        "description": (
                            "ARABIC. What the role actually is, ONE short line. "
                            "HARD LIMIT 70 characters: it renders on a WhatsApp "
                            "card with a strict size budget."
                        ),
                    },
                    "why_matched_ar": {
                        "type": "STRING",
                        "description": (
                            "ARABIC. Why this candidate fits, ONE short line. "
                            "HARD LIMIT 70 characters."
                        ),
                    },
                    "gaps_ar": {
                        "type": "STRING",
                        "description": (
                            "ARABIC. The main missing requirement(s), ONE short "
                            "line. HARD LIMIT 55 characters. Empty if none."
                        ),
                    },
                },
                "required": [
                    "job_index", "is_real_job", "company_name", "role_title",
                    "location", "salary", "match_score", "why_matched_en",
                    "gaps_en", "arabic_summary", "why_matched_ar", "gaps_ar",
                ],
            },
        }
    },
    "required": ["evaluations"],
}


SYSTEM_INSTRUCTION = """You are a precise technical recruiter screening job postings for ONE specific candidate. You will be given the candidate's CV and a numbered batch of job postings, some scraped from messy sources (Telegram messages, RSS snippets) that may be in Arabic or English.

Return exactly one evaluation object per posting, echoing its job_index.

SCORING RUBRIC -- apply it strictly and calibrate to the whole 0-100 range:
  90-100  Bullseye. Core VoIP/SIP/PBX/telephony engineering or IT application
          support, at the candidate's level, in a target location.
  75-89   Strong. Most core requirements met; minor gaps that are learnable.
          THIS IS THE ALERT THRESHOLD -- be deliberate about crossing it.
  50-74   Plausible adjacent role (general IT support, NOC, sysadmin) but
          missing the candidate's differentiators, or clearly wrong seniority.
  25-49   Same industry, wrong discipline.
  0-24    Irrelevant, or not a genuine single vacancy.

HARD RULES:
  * Score the CANDIDATE'S ACTUAL EVIDENCE, not the role's prestige. Do not
    inflate because a posting sounds impressive.
  * A role demanding 8+ years of senior/lead/principal experience must not
    exceed 60 for an early-career engineer, however good the skill overlap.
  * If the posting is a listing page, a search-results page, a CV/profile, an
    advert or a news article, set is_real_job=false and match_score=0.
  * If the text is too vague to judge (no role, no requirements), score <= 40.
  * SALARY: copy the figure the posting states, with its currency and period.
    Return an empty string when none is stated. Never estimate a market rate --
    an invented salary is worse than a missing one.

BILINGUAL OUTPUT -- both languages are required, and they are NOT translations
of each other. They serve two different cards:

  ENGLISH (why_matched_en, gaps_en) goes on the full technical card. Name the
  concrete overlapping technologies, e.g. "Requires Asterisk/FreePBX
  administration and SIP trunk troubleshooting, which maps directly to the
  candidate's Issabel PBX and SIP/IAX2 support work."

  ARABIC (arabic_summary, why_matched_ar, gaps_ar) goes on a WhatsApp card with
  a HARD size budget. Write natural, plain Arabic -- not transliterated English
  -- and keep each field to ONE short line inside its character limit. Being
  over the limit costs the reader the rest of the message, so brevity beats
  completeness here. Keep well-known technology names in Latin script (SIP,
  Issabel, Odoo, Linux); translate everything else.

  * arabic_summary  -- what the job IS.        <= 70 characters
  * why_matched_ar  -- why it suits HIM.       <= 70 characters
  * gaps_ar         -- what he is missing.     <= 55 characters, empty if none

  Extract company_name and location from the posting text itself. Use
  "Unknown" only when genuinely absent.
"""


class GeminiError(RuntimeError):
    pass


class QuotaExhausted(GeminiError):
    """The API keeps refusing with 429 -- the daily allowance is gone.

    Distinct from a transient rate-limit because the response is different:
    a transient 429 clears within its retryDelay, an exhausted daily quota
    does not clear for hours. Retrying into it burns the entire run.
    """


def _retry_delay_from(payload: dict[str, Any]) -> float | None:
    """Google returns a RetryInfo block on 429; honour it instead of guessing."""
    try:
        for detail in payload.get("error", {}).get("details", []):
            delay = detail.get("retryDelay")
            if isinstance(delay, str):
                m = re.match(r"([\d.]+)s", delay)
                if m:
                    return float(m.group(1))
    except Exception:
        pass
    return None


class GeminiEvaluator:
    def __init__(self) -> None:
        cfg = settings.gemini
        self.models: list[str] = list(cfg.get("model_chain") or ["gemini-2.5-flash"])
        self.temperature = float(cfg.get("temperature", 0.15))
        self.max_output_tokens = int(cfg.get("max_output_tokens", 8192))
        self.max_retries = int(cfg.get("max_retries", 4))
        self.backoff_base = float(cfg.get("backoff_base_seconds", 2.5))
        self.api_key = settings.gemini_api_key
        self.batch_size = int(settings.engine.get("eval_batch_size", 8))
        self.concurrency = int(settings.engine.get("eval_concurrency", 3))
        # Sticky: once a model answers, keep using it for the rest of the run.
        self._preferred: str | None = None
        # Batches run on worker threads, so the counters need a lock.
        self._counter_lock = threading.Lock()
        self.calls_made = 0
        self.tokens_used = 0
        # Set once the daily quota is clearly gone, so the remaining batches
        # fail instantly instead of each waiting out four 60-second backoffs.
        self._quota_gone = threading.Event()

    # -- transport ----------------------------------------------------------
    def _post(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        """One request, with retry/backoff. Raises GeminiError when exhausted."""
        url = f"{API_BASE}/{model}:generateContent?key={self.api_key}"
        last = ""

        for attempt in range(self.max_retries):
            try:
                resp = http_client.session().post(
                    url,
                    json=body,
                    timeout=120,
                    headers={"Content-Type": "application/json"},
                )
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}: {exc}"
                time.sleep(self.backoff_base * (2**attempt) + random.uniform(0, 1))
                continue

            with self._counter_lock:
                self.calls_made += 1
            if resp.status_code == 200:
                return resp.json()

            try:
                payload = resp.json()
            except Exception:
                payload = {}
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"

            # 400/401/403/404 are permanent for THIS model -- fall through to
            # the next one in the chain rather than burning retries.
            if resp.status_code in (400, 401, 403, 404):
                raise GeminiError(last)

            if resp.status_code in (429, 500, 502, 503, 504):
                wait = _retry_delay_from(payload) or (
                    self.backoff_base * (2**attempt) + random.uniform(0, 1.5)
                )
                log.warning(
                    "Gemini %s -> HTTP %s; retrying in %.1fs (attempt %d/%d)",
                    model, resp.status_code, wait, attempt + 1, self.max_retries,
                )
                time.sleep(min(wait, 60))
                continue

            raise GeminiError(last)

        if "429" in last:
            raise QuotaExhausted(last)
        raise GeminiError(f"exhausted retries -- {last}")

    def _generate(self, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Try the model chain until one answers."""
        chain = (
            [self._preferred] + [m for m in self.models if m != self._preferred]
            if self._preferred
            else list(self.models)
        )
        errors: list[str] = []
        # Whether any model in the chain refused for QUOTA reasons specifically.
        # This has to survive the loop. `QuotaExhausted` is a `GeminiError`, so
        # a single `except GeminiError` swallowed it here and re-raised a plain
        # GeminiError at the bottom -- which meant `evaluate()`'s
        # `except QuotaExhausted` short-circuit was unreachable code, and every
        # remaining batch waited out four backoffs against a quota that would
        # not clear for hours. Roughly 20 minutes of stalling against a
        # 25-minute job timeout, for a run that could not have succeeded.
        quota_blocked = False
        for model in chain:
            try:
                payload = self._post(model, body)
            except QuotaExhausted as exc:
                quota_blocked = True
                errors.append(f"{model}: {exc}")
                log.warning("Gemini model %s is out of quota -- trying next.", model)
                continue
            except GeminiError as exc:
                errors.append(f"{model}: {exc}")
                log.warning("Gemini model %s unavailable -- trying next.", model)
                continue
            self._preferred = model
            usage = payload.get("usageMetadata") or {}
            with self._counter_lock:
                self.tokens_used += int(usage.get("totalTokenCount") or 0)
            return payload, model

        detail = "every model in the chain failed -> " + " | ".join(errors)
        # Only when NOTHING answered: a quota-blocked model that another model
        # covers for is not an exhausted run.
        if quota_blocked:
            raise QuotaExhausted(detail)
        raise GeminiError(detail)

    # -- parsing ------------------------------------------------------------
    @staticmethod
    def _extract_json(payload: dict[str, Any]) -> dict[str, Any]:
        candidates = payload.get("candidates") or []
        if not candidates:
            block = (payload.get("promptFeedback") or {}).get("blockReason")
            raise GeminiError(f"no candidates returned (blockReason={block})")

        candidate = candidates[0]
        finish = candidate.get("finishReason")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()

        if not text:
            raise GeminiError(f"empty response (finishReason={finish})")
        if finish == "MAX_TOKENS":
            log.warning("Gemini hit MAX_TOKENS; batch may be truncated.")

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # responseSchema makes this near-impossible, but a truncated
            # response can still yield partial JSON worth salvaging.
            match = re.search(r"\{.*\}", text, re.S)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            if finish == "MAX_TOKENS":
                # Name the cause. A response cut off mid-string is not
                # "unparseable JSON" in any way the reader can act on -- it is
                # a token budget that was too small, and saying so is the
                # difference between a fix and an afternoon. Seen live on an
                # Arabic cover letter, which costs ~3x the tokens of the
                # equivalent English.
                raise GeminiError(
                    f"response truncated at the {len(text)}-character mark "
                    f"(finishReason=MAX_TOKENS) -- raise maxOutputTokens for "
                    f"this call; Arabic output needs roughly 3x the budget of "
                    f"English: {text[:120]}"
                )
            raise GeminiError(f"unparseable JSON: {text[:200]}")

    # -- public API ---------------------------------------------------------
    def evaluate_batch(self, jobs: list[JobPost], cv_text: str) -> list[Evaluation]:
        if not jobs:
            return []

        blocks = "\n".join(job.to_prompt_block(i) for i, job in enumerate(jobs))
        prompt = (
            f"=== CANDIDATE CV ===\n{cv_text}\n\n"
            f"=== JOB POSTINGS TO EVALUATE ({len(jobs)}) ===\n{blocks}\n\n"
            f"Return exactly {len(jobs)} evaluation objects, one per JOB index "
            f"0-{len(jobs) - 1}."
        )

        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
                "temperature": self.temperature,
                "maxOutputTokens": self.max_output_tokens,
                "candidateCount": 1,
            },
            # Job posts sometimes trip safety heuristics on salary/nationality
            # wording; screening them is legitimate and must not silently drop.
            "safetySettings": [
                {"category": c, "threshold": "BLOCK_ONLY_HIGH"}
                for c in (
                    "HARM_CATEGORY_HARASSMENT",
                    "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "HARM_CATEGORY_DANGEROUS_CONTENT",
                )
            ],
        }

        payload, model = self._generate(body)
        parsed = self._extract_json(payload)

        by_index: dict[int, dict[str, Any]] = {}
        for item in parsed.get("evaluations") or []:
            if not isinstance(item, dict):
                continue
            try:
                by_index[int(item.get("job_index", -1))] = item
            except (TypeError, ValueError):
                continue

        out: list[Evaluation] = []
        for i, job in enumerate(jobs):
            item = by_index.get(i)
            if item is None:
                log.warning(
                    "Gemini skipped job_index %d (%s) -- scoring 0.", i, job.title[:60]
                )
                out.append(Evaluation(
                    fingerprint=job.fingerprint,
                    company_name=job.company or "Unknown",
                    role_title=job.title or "Unknown",
                    location=job.location or "Unknown",
                    match_score=0,
                    source_platform=job.source,
                    direct_link=job.url,
                    model=model,
                    error="missing_from_response",
                ))
                continue

            ev = Evaluation.from_gemini(item, job, model)
            if item.get("is_real_job") is False:
                ev.match_score = 0
                ev.error = "not_a_real_vacancy"
            out.append(ev)
        return out

    @staticmethod
    def _failed_batch(batch: list[JobPost], reason: str) -> list[Evaluation]:
        return [
            Evaluation(
                fingerprint=j.fingerprint,
                company_name=j.company or "Unknown",
                role_title=j.title or "Unknown",
                location=j.location or "Unknown",
                match_score=0,
                source_platform=j.source,
                direct_link=j.url,
                error=reason[:200],
            )
            for j in batch
        ]

    def evaluate(
        self,
        jobs: Iterable[JobPost],
        cv_text: str,
        on_batch: Callable[[list[Evaluation]], None] | None = None,
    ) -> list[Evaluation]:
        """Evaluate every posting. Never raises.

        Batches run CONCURRENTLY (bounded by `eval_concurrency`) because each
        call spends most of its time waiting on the network -- serialising 15
        batches costs minutes of pure latency. Concurrency stays deliberately
        low so the free-tier requests-per-minute allowance is not tripped; the
        429 handler absorbs the rest.

        `on_batch` is invoked as soon as each batch lands, which lets the caller
        persist results incrementally -- so a crash or timeout half way through
        never discards the work already paid for.
        """
        jobs = list(jobs)
        if not jobs:
            return []

        batches = [
            jobs[i : i + self.batch_size]
            for i in range(0, len(jobs), self.batch_size)
        ]
        total = len(batches)
        results: list[Evaluation] = []
        done = 0

        def work(batch: list[JobPost]) -> list[Evaluation]:
            if self._quota_gone.is_set():
                return self._failed_batch(batch, "quota_exhausted")
            try:
                return self.evaluate_batch(batch, cv_text)
            except QuotaExhausted as exc:
                if not self._quota_gone.is_set():
                    self._quota_gone.set()
                    log.error(
                        "Gemini quota is exhausted (%s). Skipping the remaining "
                        "batches -- those postings stay unevaluated and will be "
                        "retried on the next run.", str(exc)[:120],
                    )
                return self._failed_batch(batch, "quota_exhausted")
            except GeminiError as exc:
                log.error("Batch failed: %s", exc)
                return self._failed_batch(batch, str(exc))
            except Exception as exc:  # never let one batch kill the run
                log.exception("Unexpected error evaluating a batch: %s", exc)
                return self._failed_batch(batch, f"{type(exc).__name__}: {exc}")

        workers = max(1, min(self.concurrency, total))
        log.info(
            "Evaluating %d posting(s) in %d batch(es), %d at a time.",
            len(jobs), total, workers,
        )

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gemini") as pool:
            for batch_result in pool.map(work, batches):
                done += 1
                results.extend(batch_result)
                if on_batch:
                    try:
                        on_batch(batch_result)
                    except Exception as exc:
                        log.warning("on_batch callback failed: %s", exc)
                log.info("Gemini progress: %d/%d batches", done, total)

        log.info(
            "Evaluation complete: %d verdicts, %d API call(s), ~%d tokens.",
            len(results), self.calls_made, self.tokens_used,
        )
        return results

    @property
    def quota_exhausted(self) -> bool:
        return self._quota_gone.is_set()

    # -- diagnostics --------------------------------------------------------
    def selftest(self) -> tuple[bool, str]:
        """Prove connectivity + schema compliance. Used by setup_wizard."""
        body = {
            "contents": [{"role": "user", "parts": [{"text": "Reply with {\"ok\":true}"}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {"ok": {"type": "BOOLEAN"}},
                    "required": ["ok"],
                },
                "maxOutputTokens": 256,
            },
        }
        try:
            payload, model = self._generate(body)
            parsed = self._extract_json(payload)
            return bool(parsed.get("ok")), model
        except GeminiError as exc:
            return False, str(exc)
