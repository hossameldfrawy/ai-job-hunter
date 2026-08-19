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
  2. TRANSPORTS  -- send_via_telegram / send_raw / http_client.get are
     replaced with recorders that deliver nothing and remember everything.
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
        self.raw: list[str] = []
        self.http: list[str] = []

    def clear(self) -> None:
        self.telegram.clear()
        self.raw.clear()
        self.http.clear()

    @property
    def total(self) -> int:
        return len(self.telegram) + len(self.raw) + len(self.http)


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
    REAL["send_raw"] = notifier.WhatsAppNotifier.send_raw
    REAL["_telegram_available"] = notifier.WhatsAppNotifier._telegram_available
    REAL["http_get"] = http_client.get

    def fake_send_via_telegram(self, message):          # noqa: ANN001
        OUTBOX.telegram.append(str(message))
        return True, "blocked-in-tests"

    def fake_send_raw(self, message):                   # noqa: ANN001
        OUTBOX.raw.append(str(message))
        return True, "blocked-in-tests"

    def fake_telegram_available():
        # Pinned True so dispatch() takes the same path on a developer machine
        # (session present) and on CI (no session). Determinism here is what
        # stops "works on my laptop" alerting bugs.
        return True

    notifier.WhatsAppNotifier.send_via_telegram = fake_send_via_telegram
    notifier.WhatsAppNotifier.send_raw = fake_send_raw
    notifier.WhatsAppNotifier._telegram_available = staticmethod(
        fake_telegram_available
    )

    # `_send_callmebot` is deliberately NOT stubbed: one test drives the real
    # method to prove the URL-budget guard bounds any message. Blocking the
    # transport underneath it gives that test something to inspect while still
    # guaranteeing nothing leaves the machine.
    def fake_http_get(url, *args, **kwargs):            # noqa: ANN001
        OUTBOX.http.append(str(url))
        raise NetworkBlockedInTests(
            f"blocked outbound HTTP GET in tests: {str(url)[:120]}"
        )

    http_client.get = fake_http_get


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
