"""
Suite-wide safety harness. Imported by pytest before any test module.

WHY THIS FILE EXISTS
--------------------
Before it did, `pytest tests/` posted FIVE real job cards to the user's own
Telegram Saved Messages on every single run.

Nothing in the suite could have told you. Every test module set DRY_RUN=true,
every assertion passed, and the run finished green -- but
`WhatsAppNotifier.send_via_telegram` had no dry-run guard, `dispatch()` called
it identically in both arms of an `if self.dry_run: ... elif ...`, and a
developer machine holds a valid MTProto session. The suite was a spam cannon
aimed at the person it was meant to protect, and it reported success.

The transport bug is fixed in notifier.py. This file exists so that the NEXT
one cannot reach the user, because relying on every future call site to
remember the guard is exactly the assumption that failed.

THREE INDEPENDENT LAYERS
------------------------
  1. ENVIRONMENT -- real credentials are replaced with obvious test values
     BEFORE config.py can read the real .env, so a stray call authenticates
     as nobody.
  2. TRANSPORTS  -- send_via_telegram, send_photo_via_telegram, send_raw,
     _send_callmebot and http_client.get are replaced with recorders that
     deliver nothing and remember everything.
  3. SOCKETS     -- connect() to any non-loopback address raises.

Layer 3 is the one that actually matters. Layers 1 and 2 can be defeated by a
test that builds its own client or reaches past the stubs; a blocked socket
cannot. Loopback stays open on purpose -- asyncio's event-loop self-pipe is a
localhost socketpair on Windows, and blocking it would break unrelated tests
for no security gain.

USING THE RECORDER
------------------
Ask for the `outbox` fixture to assert on what WOULD have been sent::

    def test_interview_mail_alerts_once(outbox):
        ...
        assert len(outbox.telegram) == 1
        assert "INTERVIEW" in outbox.telegram[0]
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Layer 1: environment
# ---------------------------------------------------------------------------
# Assignment, NOT setdefault. config.py calls load_dotenv(override=False), so
# anything already present in os.environ wins over the real .env -- but only if
# it is set before config is first imported, which is why this runs at conftest
# import time rather than in a fixture.
def _generated_vault_key() -> str:
    """A throwaway Fernet key, so tests never read secrets/vault.key."""
    try:
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode()
    except Exception:  # cryptography absent: vault tests will skip themselves
        return ""


_TEST_ENV = {
    "DRY_RUN": "true",
    "GEMINI_API_KEY": "test-key-not-a-real-credential",
    "CALLMEBOT_APIKEY": "test-apikey",
    "CALLMEBOT_API_KEY": "test-apikey",
    "WHATSAPP_PHONE": "+201234567890",
    "CV_TEXT": "VoIP engineer with SIP, Asterisk and Issabel PBX. " * 6,
    # Cleared rather than faked. Without a session string Telethon cannot sign
    # in at all, so even a test that bypasses the stubs reaches nobody.
    "TELEGRAM_STRING_SESSION": "",
    "TELEGRAM_API_ID": "",
    "TELEGRAM_API_HASH": "",
    "TELEGRAM_PHONE": "",
    # Cleared for the same reason, and for a second one: tests that depend on
    # a mailbox being configured must SAY SO. Two backend-selection tests used
    # to pass only because the developer's .env still held a dead App Password;
    # on a clean machine they failed.
    "JOB_EMAIL": "",
    "JOB_EMAIL_APP_PASSWORD": "",
    "FACEBOOK_COOKIE": "",
    "VAULT_KEY": _generated_vault_key(),
    # The seed the password-derivation tests were always written against --
    # `test_the_seed_is_not_recoverable_from_the_output` asserts this exact
    # string is absent from the output. It was never actually set, so those
    # four tests silently derived from the developer's real
    # APPLY_BASE_PASSWORD locally and failed outright on CI.
    "APPLY_BASE_PASSWORD": "TestSeed99@",
}

for _key, _value in _TEST_ENV.items():
    os.environ[_key] = _value


# ---------------------------------------------------------------------------
# Layer 3: sockets  (installed early, so even import-time traffic is caught)
# ---------------------------------------------------------------------------
class NetworkBlockedInTests(RuntimeError):
    """Raised when test code tries to open a connection to the outside world."""


_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection


def _is_loopback(address: object) -> bool:
    # AF_UNIX / abstract sockets arrive as a str or bytes path, never as a
    # remote host. asyncio's self-pipe arrives as ('127.0.0.1', port).
    if isinstance(address, (str, bytes)):
        return True
    if isinstance(address, tuple) and address:
        return str(address[0]) in _LOOPBACK
    return False


def _blocked(address: object) -> NetworkBlockedInTests:
    return NetworkBlockedInTests(
        f"The test suite tried to open a network connection to {address!r}.\n"
        "Tests must never talk to Telegram, WhatsApp, Gmail or Gemini for "
        "real -- see tests/conftest.py. Mock the transport instead."
    )


def _guarded_connect(self, address):          # type: ignore[no-untyped-def]
    if _is_loopback(address):
        return _real_connect(self, address)
    raise _blocked(address)


def _guarded_connect_ex(self, address):       # type: ignore[no-untyped-def]
    if _is_loopback(address):
        return _real_connect_ex(self, address)
    raise _blocked(address)


def _guarded_create_connection(address, *args, **kwargs):
    if _is_loopback(address):
        return _real_create_connection(address, *args, **kwargs)
    raise _blocked(address)


socket.socket.connect = _guarded_connect          # type: ignore[method-assign]
socket.socket.connect_ex = _guarded_connect_ex    # type: ignore[method-assign]
socket.create_connection = _guarded_create_connection  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Layer 2: transports
# ---------------------------------------------------------------------------
class Outbox:
    """Everything the suite tried to deliver, by channel."""

    def __init__(self) -> None:
        self.telegram: list[str] = []
        self.whatsapp: list[str] = []
        #: (path, caption) pairs -- the submission-evidence screenshots
        self.photos: list[tuple[str, str]] = []
        self.raw: list[str] = []
        self.http: list[str] = []

    def clear(self) -> None:
        self.telegram.clear()
        self.whatsapp.clear()
        self.photos.clear()
        self.raw.clear()
        self.http.clear()

    @property
    def total(self) -> int:
        return (len(self.telegram) + len(self.whatsapp) + len(self.photos)
                + len(self.raw) + len(self.http))

    def find(self, needle: str) -> list[str]:
        """Every text message on either channel containing `needle`."""
        return [m for m in (self.telegram + self.whatsapp + self.raw)
                if needle in m]


OUTBOX = Outbox()

#: The genuine implementations, stashed before they are replaced. Only
#: tests/test_no_real_sends.py should touch these -- it is the file that proves
#: the real code honours DRY_RUN, which it cannot do against a stub.
REAL: dict[str, object] = {}


def pytest_configure(config: pytest.Config) -> None:
    """Replace every outbound transport with a recorder, for the whole run."""
    import http_client
    import notifier

    REAL["send_via_telegram"] = notifier.WhatsAppNotifier.send_via_telegram
    REAL["send_photo_via_telegram"] = notifier.WhatsAppNotifier.send_photo_via_telegram
    REAL["send_raw"] = notifier.WhatsAppNotifier.send_raw
    REAL["_send_callmebot"] = notifier.WhatsAppNotifier._send_callmebot
    REAL["_telegram_available"] = notifier.WhatsAppNotifier._telegram_available
    REAL["http_get"] = http_client.get

    def fake_send_via_telegram(self, message):          # noqa: ANN001
        OUTBOX.telegram.append(str(message))
        return True, "blocked-in-tests"

    def fake_send_photo_via_telegram(self, path, caption=""):   # noqa: ANN001
        OUTBOX.photos.append((str(path), str(caption)))
        return True, "blocked-in-tests"

    def fake_send_raw(self, message):                   # noqa: ANN001
        OUTBOX.raw.append(str(message))
        return True, "blocked-in-tests"

    def fake_send_callmebot(self, message):             # noqa: ANN001
        # Stubbed as of the dual-channel review flow. Before it, the WhatsApp
        # half of every card reached `http_client.get`, which raises -- so the
        # suite could see THAT a send was attempted but never WHAT was sent,
        # and half of a two-channel feature was untestable. The real method is
        # stashed in REAL for the tests that must drive it: the URL-budget
        # guard, and the proof that it honours DRY_RUN.
        OUTBOX.whatsapp.append(str(message))
        return True, "blocked-in-tests"

    def fake_telegram_available():
        # Pinned True so dispatch() takes the same path on a developer machine
        # (session present) and on CI (no session). Determinism here is what
        # stops "works on my laptop" alerting bugs.
        return True

    notifier.WhatsAppNotifier.send_via_telegram = fake_send_via_telegram
    notifier.WhatsAppNotifier.send_photo_via_telegram = fake_send_photo_via_telegram
    notifier.WhatsAppNotifier.send_raw = fake_send_raw
    notifier.WhatsAppNotifier._send_callmebot = fake_send_callmebot
    notifier.WhatsAppNotifier._telegram_available = staticmethod(
        fake_telegram_available
    )

    def fake_http_get(url, *args, **kwargs):            # noqa: ANN001
        OUTBOX.http.append(str(url))
        raise NetworkBlockedInTests(
            f"blocked outbound HTTP GET in tests: {str(url)[:120]}"
        )

    http_client.get = fake_http_get

    # ---------------------------------------------------------------
    # Layer 4: the browser
    # ---------------------------------------------------------------
    # A real Chromium is not a network connection, so the socket block never
    # saw it -- and a test that reached the genuine `browser_context` launched
    # one, drove a live job board, and passed. That is exactly what happened
    # when the registration flow moved from `browser_page` to
    # `browser_context` and one harness only stubbed the first: the lifecycle
    # test quietly spent two seconds inside a headless browser on every run.
    #
    # Blocking it at the source means a half-stubbed harness fails loudly
    # instead of silently doing the real thing.
    try:
        import playwright.sync_api as _pw

        REAL["sync_playwright"] = _pw.sync_playwright

        def blocked_playwright(*args, **kwargs):
            raise NetworkBlockedInTests(
                "A test tried to launch a REAL browser.\n"
                "Playwright is blocked in the suite -- stub "
                "`auto_apply.browser.browser_context` (and `browser_page`) in "
                "your harness. Stubbing only one of them is how this leak got "
                "in the first time."
            )

        _pw.sync_playwright = blocked_playwright
    except Exception:            # Playwright absent on CI: nothing to block
        pass


class RecordingNotifier:
    """A dual-channel notifier double. Records everything, delivers nothing.

    Shared here rather than re-invented per file because it has to stay honest
    about ONE thing: it implements `send_dual`, so a flow that is supposed to
    reach both channels is actually observed doing so. A double that only knows
    `send_via_telegram` would quietly exercise the single-channel compatibility
    path in `review.dispatch_text` and report success for a feature that never
    WhatsApp half.
    """

    def __init__(self) -> None:
        self.telegram: list[str] = []
        self.whatsapp: list[str] = []
        self.photos: list[tuple[str, str]] = []
        self.dry_run = True

    # -- the real notifier's surface ---------------------------------------
    def send_via_telegram(self, message: str):
        self.telegram.append(str(message))
        return True, "recorded"

    def send_photo_via_telegram(self, path: str, caption: str = ""):
        self.photos.append((str(path), str(caption)))
        return True, "recorded"

    def send_raw(self, message: str):
        self.telegram.append(str(message))
        return True, "recorded"

    def send_dual(self, telegram_text, whatsapp_text=None, photo="",
                  photo_caption="", channels=("telegram", "whatsapp")):
        from notifier import DualResult

        wanted = {str(c).lower() for c in channels}
        result = DualResult()
        if "telegram" in wanted:
            self.telegram.append(str(telegram_text))
            result.telegram_ok, result.telegram_detail = True, "recorded"
            if photo:
                self.photos.append((str(photo), str(photo_caption)))
                result.photo_ok = True
        if "whatsapp" in wanted:
            self.whatsapp.append(
                str(telegram_text if whatsapp_text is None else whatsapp_text)
            )
            result.whatsapp_ok, result.whatsapp_detail = True, "recorded"
        return result

    # -- assertions helpers -------------------------------------------------
    def clear(self) -> None:
        self.telegram.clear()
        self.whatsapp.clear()
        self.photos.clear()

    @property
    def both(self) -> str:
        return "\n".join(self.telegram + self.whatsapp)

    @property
    def last_telegram(self) -> str:
        return self.telegram[-1] if self.telegram else ""

    @property
    def last_whatsapp(self) -> str:
        return self.whatsapp[-1] if self.whatsapp else ""


@pytest.fixture
def outbox() -> Outbox:
    """The recorder, emptied before each test that asks for it."""
    OUTBOX.clear()
    return OUTBOX


@pytest.fixture(autouse=True)
def _no_leaked_sends():
    """Keep the recorder from growing without bound across the whole run."""
    yield
    OUTBOX.clear()
