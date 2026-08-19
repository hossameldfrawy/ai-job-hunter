"""
Master-CV ingestion.

The CV is personal data, so the cloud deployment deliberately does NOT commit
the PDF to the repository. Instead it is supplied as a secret, which keeps the
GitHub repo public (public repos get unlimited free Actions minutes) while the
CV itself stays private.

Resolution order -- first hit wins:
  1. CV_TEXT        -- plain text, pasted straight into a secret (most robust:
                       no PDF parser needed at runtime at all)
  2. MASTER_CV_B64  -- base64 of the PDF, stored as a secret
  3. cv.paths       -- a chain of local files tried in order (config.yml).
                       The primary is the underscored filename; a spaced-name
                       copy is the fallback, so renaming one does not break
                       local runs.
  4. assets/cv_profile.json -- cached extraction from a previous run

`export_secret()` writes the sanitised text to secrets/CV_TEXT.txt and refuses
to exceed GitHub's 64 KB per-secret limit -- a secret that cannot be set is
worse than a loud failure.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from config import ROOT, settings

log = logging.getLogger(__name__)

CACHE_PATH = ROOT / "assets" / "cv_profile.json"
MAX_CV_CHARS = 12000  # generous; a 1-page CV is ~4k


class CVError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# PDF text extraction -- two independent backends, either is sufficient
# ---------------------------------------------------------------------------
def _extract_pdf(data: bytes) -> str:
    errors: list[str] = []

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        if text.strip():
            return text
        errors.append("pypdf returned no text")
    except Exception as exc:
        errors.append(f"pypdf: {type(exc).__name__}: {exc}")

    try:
        import fitz  # PyMuPDF

        with fitz.open(stream=data, filetype="pdf") as doc:
            text = "\n".join(page.get_text() for page in doc)
        if text.strip():
            return text
        errors.append("PyMuPDF returned no text")
    except Exception as exc:
        errors.append(f"PyMuPDF: {type(exc).__name__}: {exc}")

    raise CVError(
        "Could not extract text from the PDF. Tried: " + "; ".join(errors) +
        ". Fix: run `python setup_wizard.py --extract-cv` and store the output "
        "in the CV_TEXT secret instead."
    )


def _clean(text: str) -> str:
    """Normalise the glyph soup that PDF extraction produces."""
    # Private-use / replacement glyphs that CV templates emit for bullets.
    text = re.sub(r"[-�]", " ", text)
    text = text.replace("•", "- ").replace("●", "- ")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"^[\s\-]+$", "", text, flags=re.M)
    return text.strip()


@dataclass(slots=True)
class CVProfile:
    text: str
    source: str
    chars: int

    def to_prompt(self) -> str:
        return self.text[:MAX_CV_CHARS]


def _from_env_text() -> CVProfile | None:
    if not settings.cv_text_env:
        return None
    text = _clean(settings.cv_text_env)
    if len(text) < 120:
        log.warning("CV_TEXT is suspiciously short (%d chars); ignoring it.", len(text))
        return None
    return CVProfile(text=text, source="env:CV_TEXT", chars=len(text))


def _from_env_b64() -> CVProfile | None:
    if not settings.cv_b64_env:
        return None
    blob = re.sub(r"\s+", "", settings.cv_b64_env)
    try:
        data = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as exc:
        log.error("MASTER_CV_B64 is not valid base64: %s", exc)
        return None
    try:
        text = _clean(_extract_pdf(data))
    except CVError as exc:
        log.error("%s", exc)
        return None
    return CVProfile(text=text, source="env:MASTER_CV_B64", chars=len(text))


def _from_path() -> CVProfile | None:
    """Walk the configured path chain and take the first file that yields text.

    Each candidate can fail in three different ways -- missing, unreadable, or
    present but un-extractable (a scanned image PDF). All three fall through to
    the next candidate; only exhausting the whole chain is an error.
    """
    candidates = settings.cv_paths
    if not candidates:
        return None

    tried: list[str] = []
    for index, path in enumerate(candidates, 1):
        label = f"{index}/{len(candidates)} {path.name}"
        if not path.exists():
            tried.append(f"{label}: not found")
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            tried.append(f"{label}: unreadable ({exc.__class__.__name__})")
            continue

        try:
            if path.suffix.lower() == ".pdf":
                text = _clean(_extract_pdf(data))
            else:
                text = _clean(data.decode("utf-8", errors="replace"))
        except CVError as exc:
            tried.append(f"{label}: no extractable text")
            log.warning("CV candidate %s could not be parsed: %s", path.name, exc)
            continue

        if len(text) < 120:
            tried.append(f"{label}: only {len(text)} chars")
            continue

        if index > 1:
            log.info(
                "CV primary path unavailable; using fallback %s. Tried: %s",
                path.name, "; ".join(tried),
            )
        return CVProfile(text=text, source=f"file:{path.name}", chars=len(text))

    log.warning("No usable CV file in the chain. Tried: %s", "; ".join(tried))
    return None


def export_secret(path: Path | None = None) -> tuple[Path, int]:
    """Write the sanitised CV text out for use as the CV_TEXT secret.

    Returns (path, byte_size). Raises CVError if the result would exceed
    GitHub's per-secret limit, because `gh secret set` would reject it anyway
    and the failure is far more confusing at that point.
    """
    profile = load_cv()
    target = path or settings.cv_export_path
    target.parent.mkdir(parents=True, exist_ok=True)
    # Anything written under secrets/ must be unstageable, always.
    gitignore = target.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*" + chr(10), encoding="utf-8")

    payload = profile.text.strip() + chr(10)
    size = len(payload.encode("utf-8"))
    limit = settings.cv_max_secret_bytes
    if size > limit:
        raise CVError(
            f"The extracted CV is {size / 1024:.1f} KB, over the "
            f"{limit / 1024:.0f} KB GitHub secret limit. Shorten the CV, or set "
            f"cv.max_secret_bytes in config.yml if you are deploying somewhere "
            f"with a higher cap."
        )
    target.write_text(payload, encoding="utf-8")
    log.info("Wrote %s (%d bytes, %.0f%% of the secret budget).",
             target, size, 100 * size / limit)
    return target, size


def _from_cache() -> CVProfile | None:
    if not CACHE_PATH.exists():
        return None
    try:
        blob = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        text = _clean(blob.get("text", ""))
        if len(text) < 120:
            return None
        return CVProfile(text=text, source="cache", chars=len(text))
    except Exception:
        return None


def _write_cache(profile: CVProfile) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps(
                {"text": profile.text, "source": profile.source, "chars": profile.chars},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # caching is a nicety, never fatal
        log.debug("Could not write CV cache: %s", exc)


_cached: CVProfile | None = None


def load_cv(force: bool = False) -> CVProfile:
    """Resolve the master CV. Raises CVError if no source yields usable text."""
    global _cached
    if _cached is not None and not force:
        return _cached

    for loader in (_from_env_text, _from_env_b64, _from_path, _from_cache):
        try:
            profile = loader()
        except CVError:
            raise
        except Exception as exc:
            log.warning("CV loader %s failed: %s", loader.__name__, exc)
            continue
        if profile and profile.chars >= 120:
            log.info("Loaded CV from %s (%d chars).", profile.source, profile.chars)
            if profile.source != "cache":
                _write_cache(profile)
            _cached = profile
            return profile

    raise CVError(
        "No usable CV found. Provide one of:\n"
        "  * CV_TEXT       -- plain-text CV (recommended for the cloud)\n"
        "  * MASTER_CV_B64 -- base64-encoded PDF\n"
        "  * CV_PATH       -- path to a local .pdf/.txt file\n"
        "Run `python setup_wizard.py` to generate these automatically."
    )
