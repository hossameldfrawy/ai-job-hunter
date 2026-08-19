"""
Structured candidate profile, extracted once from the master CV.

The matching pipeline only ever needs the CV as free text -- Gemini reads it
whole. Applying is different: a form wants discrete fields (phone, GPA, years of
experience, a skills list) and it wants them the same way every time. Re-deriving
those from prose on every application would be slow, expensive and inconsistent.

So the CV is distilled ONCE into a structured record, cached on disk, and reused.
Gemini does the extraction because CV layouts defeat regex, but every field it
returns is overridable from config.yml -- the model should not get the last word
on the user's own phone number.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from config import ROOT, settings

log = logging.getLogger(__name__)

CACHE_PATH = ROOT / "secrets" / "cv_structured.json"

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "full_name": {"type": "STRING"},
        "email": {"type": "STRING"},
        "phone": {"type": "STRING", "description": "Primary phone, international format."},
        "location": {"type": "STRING", "description": "City, Country."},
        "linkedin_url": {"type": "STRING"},
        "headline": {
            "type": "STRING",
            "description": "One-line professional headline, max 90 characters.",
        },
        "years_experience": {
            "type": "NUMBER",
            "description": "Total professional years, as a number. Estimate from dates.",
        },
        "current_title": {"type": "STRING"},
        "current_employer": {"type": "STRING"},
        "degree": {"type": "STRING", "description": "e.g. BSc Computer Science (AI)"},
        "university": {"type": "STRING"},
        "graduation_year": {"type": "STRING"},
        "gpa": {"type": "STRING", "description": "As written, e.g. '3.43/4.0'."},
        "academic_distinction": {
            "type": "STRING",
            "description": (
                "Honours/grade classification exactly as written, e.g. "
                "'Very Good with Honors'. Empty string if absent."
            ),
        },
        "graduation_project": {
            "type": "STRING",
            "description": "Project grade if stated, e.g. 'Excellent (A+)'.",
        },
        "core_domains": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": (
                "Broad capability areas, 4-8 entries, e.g. "
                "'VoIP & Telephony (SIP, Asterisk, Issabel PBX, IVR)'."
            ),
        },
        "skills": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Flat list of concrete technologies, up to 30.",
        },
        "languages": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "e.g. 'Arabic - Native', 'English - Professional'.",
        },
        "summary": {
            "type": "STRING",
            "description": "Two-sentence professional summary for a profile bio.",
        },
    },
    "required": [
        "full_name", "email", "phone", "location", "headline",
        "years_experience", "current_title", "degree", "university",
        "gpa", "academic_distinction", "core_domains", "skills", "summary",
    ],
}


@dataclass
class CandidateProfile:
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin_url: str = ""
    headline: str = ""
    years_experience: float = 0.0
    current_title: str = ""
    current_employer: str = ""
    degree: str = ""
    university: str = ""
    graduation_year: str = ""
    gpa: str = ""
    academic_distinction: str = ""
    graduation_project: str = ""
    core_domains: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def form_values(self) -> dict[str, str]:
        """Flat map used to auto-fill registration and application forms.

        Keys are the semantic field names the form matcher looks for, not any
        one site's markup.
        """
        return {
            "full_name": self.full_name,
            "first_name": (self.full_name.split() or [""])[0],
            "last_name": " ".join(self.full_name.split()[1:]),
            "email": self.email,
            "phone": self.phone,
            "location": self.location,
            "city": self.location.split(",")[0].strip(),
            "country": (self.location.split(",")[-1].strip()
                        if "," in self.location else ""),
            "headline": self.headline,
            "current_title": self.current_title,
            "current_employer": self.current_employer,
            "years_experience": str(int(self.years_experience or 0)),
            "degree": self.degree,
            "university": self.university,
            "graduation_year": self.graduation_year,
            "gpa": self.gpa,
            "linkedin": self.linkedin_url,
            "summary": self.summary,
            # `bio` and `username` are REGISTRATION fields -- they only appear
            # on signup forms, and leaving them blank is the difference between
            # a profile a recruiter can read and an empty stub that never
            # surfaces in a search.
            "bio": self.summary,
            "headline": self.headline,
            "username": self.username,
            "skills": ", ".join(self.skills[:20]),
        }

    @property
    def username(self) -> str:
        """A handle for the few boards that want one instead of the email.

        Derived from the email's local part, then the name -- the same handle
        the candidate would have picked, so it stays recognisable to them.
        """
        import re

        base = (self.email.split("@")[0] if "@" in self.email
                else self.full_name)
        handle = re.sub(r"[^a-z0-9]+", "", base.lower())
        if len(handle) < 4:
            handle = re.sub(r"[^a-z0-9]+", "", self.full_name.lower())
        return handle[:24]

    def highlights(self) -> str:
        """The bits worth repeating in a cover letter or a profile bio."""
        bits: list[str] = []
        if self.academic_distinction or self.gpa:
            bits.append(
                f"{self.degree}"
                + (f", {self.academic_distinction}" if self.academic_distinction else "")
                + (f" (GPA {self.gpa})" if self.gpa else "")
            )
        if self.graduation_project:
            bits.append(f"Graduation project: {self.graduation_project}")
        if self.core_domains:
            bits.append("Core domains: " + "; ".join(self.core_domains[:6]))
        return "\n".join(bits)


def _from_gemini(cv_text: str) -> dict[str, Any]:
    from evaluator import GeminiEvaluator

    evaluator = GeminiEvaluator()
    body = {
        "systemInstruction": {"parts": [{"text":
            "Extract a structured profile from this CV. Copy values verbatim "
            "where the CV states them -- especially grades, GPA and phone "
            "numbers. Never invent a value; return an empty string instead. "
            "Estimate years_experience from the employment dates."
        }]},
        "contents": [{"role": "user", "parts": [{"text": cv_text[:14000]}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": EXTRACTION_SCHEMA,
            "temperature": 0.0,
            "maxOutputTokens": 4096,
        },
    }
    payload, model = evaluator._generate(body)  # noqa: SLF001 - internal by design
    log.info("Extracted structured CV profile with %s.", model)
    return evaluator._extract_json(payload)    # noqa: SLF001


def _apply_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """config.yml wins over the model for identity fields."""
    overrides = (settings.raw.get("auto_apply", {}) or {}).get("identity", {}) or {}
    for key, value in overrides.items():
        if value not in (None, ""):
            data[key] = value
    return data


_cached: CandidateProfile | None = None


def load_candidate(force: bool = False) -> CandidateProfile:
    """Return the structured profile, extracting it only when necessary."""
    global _cached
    if _cached is not None and not force:
        return _cached

    if CACHE_PATH.exists() and not force:
        try:
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            _cached = CandidateProfile(**_apply_overrides(data))
            return _cached
        except Exception as exc:
            log.warning("Structured CV cache unreadable (%s); re-extracting.", exc)

    from cv_profile import load_cv

    raw = _from_gemini(load_cv().to_prompt())
    # Drop anything the schema does not know about rather than exploding.
    known = set(CandidateProfile().to_dict())
    data = {k: v for k, v in raw.items() if k in known}
    data = _apply_overrides(data)

    profile = CandidateProfile(**data)
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        (CACHE_PATH.parent / ".gitignore").write_text("*\n", encoding="utf-8")
        CACHE_PATH.write_text(
            json.dumps(profile.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        log.debug("Could not cache the structured profile: %s", exc)

    _cached = profile
    return profile
