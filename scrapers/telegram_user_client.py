"""
Telegram MTProto **user client** -- reads the chats you have actually joined.

This is a categorically different capability from `telegram_web.py`. That module
scrapes `t.me/s/<channel>`, which only works for PUBLIC broadcast channels that
have the web preview switched on. This one logs in as *you*, over Telegram's
native MTProto protocol, and can therefore read:

  * private recruitment groups you were invited to
  * supergroups (which have no web preview at all)
  * channels that disabled the public preview
  * anything else in your dialog list

Two modes, and the distinction matters for deployment:

  POLL  (`TelegramUserClientScraper`) -- walks your dialogs, pulls messages
        newer than a per-chat cursor, returns them to the batch pipeline.
        This is what runs under GitHub Actions.

  LIVE  (`TelegramLiveListener`) -- holds an open connection and reacts to
        `events.NewMessage` the instant a message arrives. Needs a persistent
        process (Docker / VPS / `main.py --live`); it cannot work on a
        scheduled Actions run, which is killed after each execution.

On account safety: reading chats you already belong to, at human-ish rates, is
about as low-risk as automation on Telegram gets -- but it is not zero-risk.
Telegram rate-limits and can restrict accounts for automated behaviour, so this
module keeps a deliberate distance from the limits: it paces requests between
dialogs, obeys `FloodWaitError` rather than retrying through it, reads only new
messages via cursors, and never sends, joins, or reacts to anything.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

from config import settings
from models import JobPost
from scrapers.base import BaseScraper, clean, derive_post_title, first_url

log = logging.getLogger(__name__)


class TelegramAuthError(RuntimeError):
    """Raised when the user client cannot authenticate without a human."""


def telethon_available() -> bool:
    try:
        import telethon  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Content gates
# ---------------------------------------------------------------------------
# A joined group is mostly conversation. These two gates decide what is even
# worth handing to the rest of the pipeline.

HIRING_TERMS = [
    # English
    "hiring", "we are hiring", "vacancy", "vacancies", "job", "jobs", "opening",
    "opportunity", "position", "recruit", "recruiting", "recruitment", "apply",
    "send your cv", "send cv", "share your cv", "resume", "urgently required",
    "required", "wanted", "join our team", "now hiring", "career",
    # Arabic
    "مطلوب", "وظيفة", "وظائف", "شاغر", "شواغر", "فرصة عمل", "فرص عمل",
    "التقديم", "للتقديم", "ارسال السيرة", "السيرة الذاتية", "نبحث عن",
    "تعيين", "يعلن عن", "مطلوبين",
]

TECH_TERMS = [
    # the user's core stack
    "voip", "sip", "iax", "issabel", "asterisk", "freepbx", "pbx", "softphone",
    "ivr", "telephony", "telecom", "telecommunication", "unified communications",
    "contact center", "contact centre", "call center", "call centre",
    "kamailio", "opensips", "3cx", "genesys", "avaya", "cisco", "sbc",
    "session border controller", "trunk", "dialer",
    # adjacent IT
    "it support", "application support", "technical support", "service desk",
    "help desk", "helpdesk", "it specialist", "system administrator", "sysadmin",
    "network engineer", "noc", "linux", "windows server", "odoo", "erp",
    "pos", "point of sale", "cctv", "python", "itsm", "devops", "server",
    "information technology", "software", "developer", "engineer",
    # Arabic
    "دعم فني", "الدعم الفني", "تقنية معلومات", "تكنولوجيا المعلومات", "شبكات",
    "مبرمج", "برمجة", "اتصالات", "سيرفر", "لينكس", "انظمة", "أنظمة", "مهندس",
]


def _build_matcher(terms: Sequence[str]) -> re.Pattern[str]:
    """One alternation for the whole term list -- far faster than N searches.

    Latin terms get word boundaries so `sip` cannot match inside `gossip`.
    Arabic terms do not, because `\\b` behaves badly around Arabic script.
    """
    parts: list[str] = []
    for term in terms:
        escaped = re.escape(term.strip().lower()).replace(r"\ ", r"\s+")
        if not escaped:
            continue
        if re.search(r"[a-z]", term.lower()):
            parts.append(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")
        else:
            parts.append(escaped)
    return re.compile("|".join(parts), re.I | re.U)


HIRING_RE = _build_matcher(HIRING_TERMS)
TECH_RE = _build_matcher(TECH_TERMS)

MIN_POST_CHARS = 55


@dataclass(slots=True)
class ChatFilter:
    """Which of your dialogs to read."""

    include: list[str]
    exclude: list[str]
    include_private: bool
    include_groups: bool
    include_channels: bool

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "ChatFilter":
        return cls(
            include=[str(x).lower().lstrip("@") for x in (cfg.get("include_chats") or [])],
            exclude=[str(x).lower().lstrip("@") for x in (cfg.get("exclude_chats") or [])],
            include_private=bool(cfg.get("include_private_chats", False)),
            include_groups=bool(cfg.get("include_groups", True)),
            include_channels=bool(cfg.get("include_channels", True)),
        )

    def allows(self, dialog: Any) -> bool:
        title = (getattr(dialog, "name", "") or "").lower()
        username = (getattr(getattr(dialog, "entity", None), "username", "") or "").lower()
        chat_id = str(getattr(dialog, "id", ""))
        identity = {title, username, chat_id}

        # Explicit exclusions always win.
        for token in self.exclude:
            if token in title or token == username or token == chat_id:
                return False

        if dialog.is_user:
            if not self.include_private:
                return False
        elif dialog.is_channel and not dialog.is_group:
            if not self.include_channels:
                return False
        elif not self.include_groups:
            return False

        # Empty include list == every dialog that passed the type gate.
        if not self.include:
            return True
        for token in self.include:
            if token in title or token == username or token == chat_id:
                return True
        return False


def is_hiring_post(text: str, require_tech: bool = True) -> bool:
    """Cheap gate: does this message look like a technical job advert?"""
    if not text or len(text) < MIN_POST_CHARS:
        return False
    if not HIRING_RE.search(text):
        return False
    if require_tech and not TECH_RE.search(text):
        return False
    return True


def tech_hits(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in TECH_RE.finditer(text or "")})[:8]


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------
def build_client(interactive: bool = False) -> Any:
    """Create (not connect) a TelegramClient from configured credentials."""
    if not telethon_available():
        raise TelegramAuthError(
            "Telethon is not installed. Run: pip install -r requirements.txt"
        )

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise TelegramAuthError(
            "TELEGRAM_API_ID and TELEGRAM_API_HASH are not set. Get them from "
            "https://my.telegram.org -> API development tools."
        )
    try:
        api_id = int(settings.telegram_api_id)
    except ValueError as exc:
        raise TelegramAuthError(
            f"TELEGRAM_API_ID must be a number, got {settings.telegram_api_id!r}"
        ) from exc

    # Identify as a normal desktop client; the defaults advertise Telethon.
    kwargs: dict[str, Any] = dict(
        device_model="AI Job Hunter",
        system_version="Windows 11",
        app_version="1.0",
        lang_code="en",
        system_lang_code="en",
    )

    if settings.telegram_session:
        return TelegramClient(
            StringSession(settings.telegram_session),
            api_id,
            settings.telegram_api_hash,
            **kwargs,
        )

    path = settings.telegram_session_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if not interactive and not path.with_suffix(".session").exists():
        raise TelegramAuthError(
            "No Telegram session available.\n"
            "  Run once, interactively:  python auth_telegram.py\n"
            "  That produces a local .session file AND a TELEGRAM_STRING_SESSION\n"
            "  value for headless/cloud deployment."
        )
    return TelegramClient(str(path), api_id, settings.telegram_api_hash, **kwargs)


async def ensure_authorized(client: Any) -> None:
    """Connect and confirm the session is still valid. Never prompts."""
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        raise TelegramAuthError(
            "The Telegram session is invalid, expired or was revoked.\n"
            "Regenerate it with:  python auth_telegram.py"
        )


# ---------------------------------------------------------------------------
# Message -> JobPost
# ---------------------------------------------------------------------------
def _chat_slug(dialog_name: str, username: str | None, chat_id: Any) -> str:
    if username:
        return username
    slug = re.sub(r"[^\w؀-ۿ]+", "_", (dialog_name or "").strip())[:40]
    return slug.strip("_") or f"chat{chat_id}"


def _message_link(chat_id: Any, username: str | None, message_id: int) -> str:
    """Build a t.me permalink, public or private."""
    if username:
        return f"https://t.me/{username}/{message_id}"
    # Supergroups/channels are -100XXXXXXXXXX; the private link drops the -100.
    raw = str(chat_id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/{message_id}"
    return ""


def message_to_job(
    text: str,
    *,
    chat_name: str,
    username: str | None,
    chat_id: Any,
    message_id: int,
    posted_at: datetime | None,
) -> JobPost:
    permalink = _message_link(chat_id, username, message_id)
    external = first_url(text)
    if external and "t.me/" in external:
        external = ""

    return JobPost(
        source=f"telegram_user:{_chat_slug(chat_name, username, chat_id)}",
        title=derive_post_title(text),
        company="",       # Gemini extracts these from the message body
        location="",
        url=external or permalink,
        description=clean(text, 4000),
        posted_at=posted_at,
        raw={
            "chat": chat_name,
            "chat_id": str(chat_id),
            "message_id": message_id,
            "permalink": permalink,
            "tech_hits": tech_hits(text),
        },
    )


# ---------------------------------------------------------------------------
# POLL mode -- the batch pipeline source
# ---------------------------------------------------------------------------
class TelegramUserClientScraper(BaseScraper):
    """Reads new messages from every joined dialog the filter allows."""

    name = "telegram_user"

    def __init__(self, cfg: dict[str, Any], timeout: int = 25, db: Any = None):
        super().__init__(cfg, timeout)
        self.db = db
        self.filter = ChatFilter.from_config(self.cfg)
        self.messages_per_dialog = int(self.cfg.get("messages_per_dialog", 60))
        self.max_dialogs = int(self.cfg.get("max_dialogs", 250))
        self.lookback_hours = int(self.cfg.get("lookback_hours", 72))
        self.require_tech = bool(self.cfg.get("require_tech_match", True))
        self.dialog_pause = float(self.cfg.get("pause_between_chats_seconds", 0.4))

    # -- per-chat cursors, stored in the same SQLite the pipeline persists ---
    def _cursor(self, chat_id: Any) -> int:
        if not self.db:
            return 0
        try:
            return int(self.db.get_meta(f"tg:lastid:{chat_id}", "0") or 0)
        except (ValueError, TypeError):
            return 0

    def _save_cursor(self, chat_id: Any, message_id: int) -> None:
        if self.db and message_id:
            self.db.set_meta(f"tg:lastid:{chat_id}", str(message_id))

    async def _collect_async(self) -> list[JobPost]:
        from telethon.errors import FloodWaitError

        client = build_client()
        jobs: list[JobPost] = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)

        await ensure_authorized(client)
        try:
            me = await client.get_me()
            log.info(
                "[telegram_user] signed in as %s (id %s)",
                getattr(me, "username", None) or getattr(me, "first_name", "?"),
                getattr(me, "id", "?"),
            )

            dialogs = []
            async for dialog in client.iter_dialogs(limit=self.max_dialogs):
                if self.filter.allows(dialog):
                    dialogs.append(dialog)
            log.info(
                "[telegram_user] monitoring %d of your dialogs (cap %d)",
                len(dialogs), self.max_dialogs,
            )

            scanned = 0
            for dialog in dialogs:
                chat_id = dialog.id
                username = getattr(dialog.entity, "username", None)
                cursor = self._cursor(chat_id)
                newest = cursor
                found_here = 0

                try:
                    async for msg in client.iter_messages(
                        dialog.entity,
                        limit=self.messages_per_dialog,
                        min_id=cursor,          # only what we have not read
                    ):
                        scanned += 1
                        newest = max(newest, msg.id)

                        when = getattr(msg, "date", None)
                        if when and when.astimezone(timezone.utc) < cutoff:
                            break  # messages arrive newest-first

                        text = (msg.message or "").strip()
                        if not is_hiring_post(text, self.require_tech):
                            continue

                        jobs.append(message_to_job(
                            text,
                            chat_name=dialog.name or "",
                            username=username,
                            chat_id=chat_id,
                            message_id=msg.id,
                            posted_at=when,
                        ))
                        found_here += 1

                except FloodWaitError as exc:
                    # Telegram is explicitly telling us to slow down. Honour it
                    # and move on rather than hammering into a restriction.
                    log.warning(
                        "[telegram_user] flood-wait %ss on %r -- skipping the "
                        "rest of this run.", exc.seconds, dialog.name,
                    )
                    break
                except Exception as exc:
                    log.warning(
                        "[telegram_user] could not read %r: %s: %s",
                        dialog.name, type(exc).__name__, exc,
                    )
                    continue

                self._save_cursor(chat_id, newest)
                if found_here:
                    log.info(
                        "[telegram_user] %s -> %d hiring post(s)",
                        dialog.name, found_here,
                    )
                if self.dialog_pause:
                    await asyncio.sleep(self.dialog_pause)

            log.info(
                "[telegram_user] scanned %d message(s), kept %d hiring post(s)",
                scanned, len(jobs),
            )
        finally:
            await client.disconnect()
        return jobs

    def collect(self) -> Iterable[JobPost]:
        if not settings.telegram_ready:
            log.info(
                "[telegram_user] Not configured -- no API credentials or session. "
                "Run `python auth_telegram.py` to enable private-group monitoring."
            )
            return []
        if not telethon_available():
            log.warning("[telegram_user] Telethon is not installed.")
            return []

        # Scrapers run on worker threads, which have no event loop of their own.
        try:
            return asyncio.run(self._collect_async())
        except TelegramAuthError as exc:
            log.error("[telegram_user] %s", exc)
            return []
        except RuntimeError as exc:
            log.warning("[telegram_user] event-loop problem: %s", exc)
            return []


# ---------------------------------------------------------------------------
# LIVE mode -- react the instant a message lands
# ---------------------------------------------------------------------------
class TelegramLiveListener:
    """Holds an open MTProto connection and reacts to new messages.

    Requires a long-lived process. `on_job` is called (from the event loop) for
    every message that clears the hiring gate; the caller decides what to do
    with it -- in practice: dedupe, score with Gemini, and alert.
    """

    def __init__(self, cfg: dict[str, Any], on_job: Callable[[JobPost], None]):
        self.cfg = cfg or {}
        self.on_job = on_job
        self.filter = ChatFilter.from_config(self.cfg)
        self.require_tech = bool(self.cfg.get("require_tech_match", True))
        self._seen_ids: set[tuple[Any, int]] = set()
        self.messages_seen = 0
        self.jobs_emitted = 0

    def _allowed_chat(self, chat: Any, is_private: bool, is_group: bool) -> bool:
        title = (getattr(chat, "title", "") or getattr(chat, "first_name", "") or "").lower()
        username = (getattr(chat, "username", "") or "").lower()
        chat_id = str(getattr(chat, "id", ""))

        for token in self.filter.exclude:
            if token in title or token == username or token == chat_id:
                return False
        if is_private and not self.filter.include_private:
            return False
        if is_group and not self.filter.include_groups:
            return False
        if not self.filter.include:
            return True
        return any(
            token in title or token == username or token == chat_id
            for token in self.filter.include
        )

    async def run(self) -> None:
        from telethon import events

        client = build_client()
        await ensure_authorized(client)
        me = await client.get_me()
        log.info(
            "LIVE listener attached to Telegram as %s -- reacting to new messages "
            "in real time.",
            getattr(me, "username", None) or getattr(me, "first_name", "?"),
        )

        @client.on(events.NewMessage)
        async def handler(event: Any) -> None:  # noqa: ANN401
            try:
                self.messages_seen += 1
                text = (event.raw_text or "").strip()
                if not is_hiring_post(text, self.require_tech):
                    return

                chat = await event.get_chat()
                if not self._allowed_chat(
                    chat, bool(event.is_private), bool(event.is_group)
                ):
                    return

                key = (event.chat_id, event.id)
                if key in self._seen_ids:
                    return
                self._seen_ids.add(key)
                if len(self._seen_ids) > 5000:
                    self._seen_ids.clear()

                job = message_to_job(
                    text,
                    chat_name=getattr(chat, "title", "")
                    or getattr(chat, "first_name", "")
                    or "",
                    username=getattr(chat, "username", None),
                    chat_id=event.chat_id,
                    message_id=event.id,
                    posted_at=getattr(event.message, "date", None),
                )
                self.jobs_emitted += 1
                log.info("LIVE hit in %r: %s", job.raw.get("chat"), job.title[:80])

                # The callback does network I/O (Gemini, WhatsApp). Run it off
                # the event loop so the listener keeps consuming updates.
                await asyncio.to_thread(self.on_job, job)
            except Exception as exc:
                log.exception("LIVE handler error: %s", exc)

        try:
            await client.run_until_disconnected()
        finally:
            await client.disconnect()

    def run_forever(self) -> None:
        """Blocking entry point, with reconnect-on-drop."""
        backoff = 5
        while True:
            try:
                asyncio.run(self.run())
                log.warning("Telegram connection closed; reconnecting...")
            except TelegramAuthError as exc:
                log.error("%s", exc)
                return
            except KeyboardInterrupt:
                return
            except Exception as exc:
                log.error("LIVE listener crashed: %s: %s", type(exc).__name__, exc)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
