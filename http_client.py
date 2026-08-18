"""
Hardened HTTP layer shared by every scraper.

Job boards rate-limit and fingerprint aggressively, and a scheduled cloud job
gets no second chance -- so every outbound request goes through here:

  * connection pooling + urllib3 retry on transient failures
  * per-host token-bucket throttling (never hammer one origin)
  * rotating desktop User-Agents and realistic browser headers
  * jitter, so 8 concurrent scrapers don't fire in lockstep
  * a hard "circuit breaker": a host that fails repeatedly is skipped for the
    rest of the run instead of burning the whole time budget on timeouts
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "no-cache",
}

# Minimum seconds between requests to the same host.
HOST_MIN_INTERVAL: dict[str, float] = {
    "www.linkedin.com": 2.5,
    "linkedin.com": 2.5,
    "t.me": 1.0,
    "news.google.com": 1.5,
    "www.bing.com": 2.0,
    "duckduckgo.com": 2.0,
}
DEFAULT_MIN_INTERVAL = 0.6

# Consecutive failures before a host is skipped for the remainder of the run.
CIRCUIT_TRIP_THRESHOLD = 4


class _HostThrottle:
    """Token-bucket-ish per-host pacing, safe across scraper threads."""

    def __init__(self) -> None:
        self._last: dict[str, float] = defaultdict(float)
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._guard = threading.Lock()

    def wait(self, host: str) -> None:
        with self._guard:
            lock = self._locks[host]
        with lock:
            interval = HOST_MIN_INTERVAL.get(host, DEFAULT_MIN_INTERVAL)
            elapsed = time.monotonic() - self._last[host]
            delay = interval - elapsed
            if delay > 0:
                time.sleep(delay + random.uniform(0.05, 0.35))
            self._last[host] = time.monotonic()


class _CircuitBreaker:
    def __init__(self) -> None:
        self._fails: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def is_open(self, host: str) -> bool:
        with self._lock:
            return self._fails[host] >= CIRCUIT_TRIP_THRESHOLD

    def record_failure(self, host: str) -> None:
        with self._lock:
            self._fails[host] += 1
            if self._fails[host] == CIRCUIT_TRIP_THRESHOLD:
                log.warning(
                    "Circuit breaker OPEN for %s after %d consecutive failures; "
                    "skipping it for the rest of this run.",
                    host, CIRCUIT_TRIP_THRESHOLD,
                )

    def record_success(self, host: str) -> None:
        with self._lock:
            self._fails[host] = 0


class CircuitOpen(RuntimeError):
    """Raised when a host has been disabled by the circuit breaker."""


_throttle = _HostThrottle()
_breaker = _CircuitBreaker()


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=2,
        read=2,
        status=2,
        backoff_factor=1.2,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD", "POST"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=24, pool_maxsize=24)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(BASE_HEADERS)
    return session


_local = threading.local()


def session() -> requests.Session:
    """One pooled Session per thread (requests.Session is not thread-safe)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = _build_session()
        _local.session = s
    return s


def get(
    url: str,
    *,
    timeout: int = 25,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    allow_redirects: bool = True,
    respect_circuit: bool = True,
) -> requests.Response:
    """Throttled, retried, UA-rotated GET.

    Raises CircuitOpen if the host has already failed too many times this run.
    """
    host = urlsplit(url).netloc.lower()
    if respect_circuit and _breaker.is_open(host):
        raise CircuitOpen(f"circuit open for {host}")

    _throttle.wait(host)
    hdrs = {"User-Agent": random.choice(USER_AGENTS)}
    if headers:
        hdrs.update(headers)

    try:
        resp = session().get(
            url,
            timeout=timeout,
            headers=hdrs,
            params=params,
            allow_redirects=allow_redirects,
        )
    except Exception:
        _breaker.record_failure(host)
        raise

    if resp.status_code >= 500 or resp.status_code in (403, 429):
        _breaker.record_failure(host)
    else:
        _breaker.record_success(host)
    return resp


def get_text(url: str, **kwargs: Any) -> str:
    """GET returning decoded text, or "" on any failure. Never raises."""
    try:
        resp = get(url, **kwargs)
    except CircuitOpen:
        return ""
    except Exception as exc:
        log.debug("GET failed %s -> %s: %s", url, type(exc).__name__, exc)
        return ""
    if resp.status_code != 200:
        log.debug("GET %s -> HTTP %s", url, resp.status_code)
        return ""
    # requests' charset guess is wrong often enough on Arabic pages to matter.
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def get_json(url: str, **kwargs: Any) -> Any:
    """GET returning parsed JSON, or None on any failure. Never raises."""
    try:
        resp = get(url, headers={"Accept": "application/json"}, **kwargs)
    except CircuitOpen:
        return None
    except Exception as exc:
        log.debug("GET(json) failed %s -> %s: %s", url, type(exc).__name__, exc)
        return None
    if resp.status_code != 200:
        log.debug("GET(json) %s -> HTTP %s", url, resp.status_code)
        return None
    try:
        return resp.json()
    except Exception:
        log.debug("GET(json) %s -> body was not JSON", url)
        return None


def reset_circuits() -> None:
    """Clear breaker state (used between daemon-mode iterations)."""
    global _breaker
    _breaker = _CircuitBreaker()
