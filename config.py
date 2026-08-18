"""
Configuration loader.

Two layers, in strict priority order:
  1. SECRETS  -> environment variables (.env locally, GitHub/Render secrets in
                 the cloud). Never written to disk, never committed.
  2. TUNING   -> config.yml, safe to commit and edit freely.

Import `settings` and use it; everything is validated at construction time so
the process fails loudly at startup rather than silently mid-run.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:  # optional: local dev convenience only
    from dotenv import load_dotenv

    load_dotenv(override=False)
except Exception:  # pragma: no cover - dotenv is not required in the cloud
    pass

ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Windows consoles default to cp1252 and explode on Arabic job posts.
# Force UTF-8 on every stream before anything tries to print.
# ---------------------------------------------------------------------------
def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8()


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "y"}


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


class ConfigError(RuntimeError):
    """Raised when the process cannot possibly do useful work."""


@dataclass(slots=True)
class Settings:
    # -- secrets ------------------------------------------------------------
    gemini_api_key: str
    callmebot_apikey: str
    whatsapp_phone: str

    # CV is supplied as one of: plain text secret, base64 PDF secret, or a
    # local file. Resolved lazily by cv_profile.py.
    cv_text_env: str = ""
    cv_b64_env: str = ""
    cv_path: str = ""

    # optional
    telegram_api_id: str = ""
    telegram_api_hash: str = ""
    telegram_session: str = ""
    facebook_cookie: str = ""

    # -- runtime ------------------------------------------------------------
    db_path: Path = field(default_factory=lambda: ROOT / "state" / "jobs.db")
    dry_run: bool = False
    log_level: str = "INFO"
    run_mode: str = "once"

    # -- tuning (config.yml) ------------------------------------------------
    raw: dict[str, Any] = field(default_factory=dict)

    # ---- convenience accessors -------------------------------------------
    @property
    def engine(self) -> dict[str, Any]:
        return self.raw.get("engine", {})

    @property
    def gemini(self) -> dict[str, Any]:
        return self.raw.get("gemini", {})

    @property
    def profile(self) -> dict[str, Any]:
        return self.raw.get("profile", {})

    @property
    def notifications(self) -> dict[str, Any]:
        return self.raw.get("notifications", {})

    @property
    def match_threshold(self) -> int:
        return int(self.engine.get("match_threshold", 75))

    @property
    def http_timeout(self) -> int:
        return int(self.engine.get("http_timeout", 25))

    def source(self, name: str) -> dict[str, Any]:
        """Config block for one ingestion source ({} if absent)."""
        return self.raw.get(name, {}) or {}

    def source_enabled(self, name: str) -> bool:
        blk = self.source(name)
        if not blk:
            return False
        # Env kill-switch, e.g. DISABLE_LINKEDIN=1 for an emergency shutoff.
        if _env_bool(f"DISABLE_{name.upper()}"):
            return False
        return bool(blk.get("enabled", True))

    # ---- validation -------------------------------------------------------
    def validate(self) -> list[str]:
        """Return a list of fatal problems (empty list == good to go)."""
        problems: list[str] = []
        if not self.gemini_api_key:
            problems.append("GEMINI_API_KEY is not set.")
        if not self.callmebot_apikey:
            problems.append("CALLMEBOT_APIKEY is not set.")
        if not self.whatsapp_phone:
            problems.append("WHATSAPP_PHONE is not set.")
        elif not self.whatsapp_phone.startswith("+"):
            problems.append(
                f"WHATSAPP_PHONE must be in international format starting with "
                f"'+' (got {self.whatsapp_phone!r})."
            )
        if not (self.cv_text_env or self.cv_b64_env or self.cv_path):
            problems.append(
                "No CV source. Set CV_TEXT, MASTER_CV_B64, or CV_PATH."
            )
        return problems

    def require_valid(self) -> None:
        problems = self.validate()
        if problems:
            raise ConfigError(
                "Configuration is incomplete:\n  - " + "\n  - ".join(problems)
            )


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_settings(config_path: Path | None = None) -> Settings:
    raw = _load_yaml(config_path or ROOT / "config.yml")

    default_cv = ROOT / "assets" / "master_cv.pdf"
    cv_path = _env("CV_PATH") or (str(default_cv) if default_cv.exists() else "")

    db_path = Path(_env("DB_PATH") or (ROOT / "state" / "jobs.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    s = Settings(
        gemini_api_key=_env("GEMINI_API_KEY"),
        callmebot_apikey=_env("CALLMEBOT_APIKEY"),
        whatsapp_phone=_env("WHATSAPP_PHONE"),
        cv_text_env=_env("CV_TEXT"),
        cv_b64_env=_env("MASTER_CV_B64"),
        cv_path=cv_path,
        telegram_api_id=_env("TELEGRAM_API_ID"),
        telegram_api_hash=_env("TELEGRAM_API_HASH"),
        telegram_session=_env("TELEGRAM_SESSION"),
        facebook_cookie=_env("FACEBOOK_COOKIE"),
        db_path=db_path,
        dry_run=_env_bool("DRY_RUN"),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        run_mode=_env("RUN_MODE", "once").lower(),
        raw=raw,
    )

    # Env overrides for the two knobs most likely to need emergency tuning.
    if _env("MATCH_THRESHOLD"):
        s.raw.setdefault("engine", {})["match_threshold"] = _env_int(
            "MATCH_THRESHOLD", 75
        )
    if _env("MAX_ALERTS_PER_RUN"):
        s.raw.setdefault("engine", {})["max_alerts_per_run"] = _env_int(
            "MAX_ALERTS_PER_RUN", 12
        )
    return s


settings = load_settings()
