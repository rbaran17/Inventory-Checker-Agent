"""Access control helpers: key comparison, derived keys, attempt limits and log redaction.

There are two roles. The **laptop** opens the link printed in the terminal (it carries the access key) and
can do everything. The **phone** opens the link in the QR code, which carries a key *derived* from the access
key; it can scan, see its results and remove scans, and nothing else. Browsers keep a session cookie that is
also derived, so the access key itself is never stored in a cookie.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import re
import time
from collections import OrderedDict, deque

LAPTOP_COOKIE = "ic_session"
PHONE_COOKIE = "ic_phone"
COOKIE_MAX_AGE = 12 * 3600  # one working day; reopen the terminal link or rescan the QR code after that

# What the phone role may call. Everything else needs the laptop's key.
_PHONE_ROUTES = [(m, re.compile(p)) for m, p in (
    ("GET", r"/scan"),
    ("POST", r"/api/ping"),
    ("POST", r"/api/scans"),
    ("GET", r"/api/phone/scans"),
    ("GET", r"/api/scans/[^/]+/frames/\d+"),
    ("DELETE", r"/api/scans/[^/]+"),
    ("DELETE", r"/api/scans/[^/]+/objects/[^/]+"),
)]


def same(a: str, b: str) -> bool:
    """Constant-time comparison that accepts any text (``compare_digest`` on ``str`` rejects non-ASCII)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def derive(access_key: str, purpose: str) -> str:
    """A key for one purpose that cannot be turned back into the access key."""
    digest = hmac.new(access_key.encode("utf-8"), f"inventory-checker:{purpose}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:32]


def phone_may(method: str, path: str) -> bool:
    return any(method == m and p.fullmatch(path) for m, p in _PHONE_ROUTES)


UNKNOWN_CALLER = "unknown"


def caller_key(host: str | None) -> str:
    """The attempt-limiter bucket for a client address.

    The address is whatever the server (or a proxy it was told to trust, see ``FORWARDED_ALLOW_IPS``) reports,
    so it is validated rather than used as-is: an IPv4 address is its own bucket; an IPv6 address shares one
    with its /64, because a single subscriber holds a whole /64 and could otherwise use a fresh address for
    every guess; anything that is not an IP address goes into one fixed bucket. The result is always short,
    so a caller cannot make the limiter keep long strings.
    """
    text = (host or "").strip()
    if not text or len(text) > 64:  # the longest textual IPv6 address with a zone is well under this
        return UNKNOWN_CALLER
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])  # drop an IPv6 zone such as %eth0
    except ValueError:
        return UNKNOWN_CALLER
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is None:
            return str(ipaddress.ip_network((int(ip) >> 64 << 64, 64)))
        ip = ip.ipv4_mapped
    return str(ip)


class AttemptLimiter:
    """Counts events per caller in a sliding window, e.g. wrong access keys per client address."""

    def __init__(self, limit: int, window_seconds: float, max_callers: int = 10_000) -> None:
        self.limit, self.window, self.max_callers = limit, window_seconds, max_callers
        self._events: OrderedDict[str, deque[float]] = OrderedDict()

    def _recent(self, caller: str, now: float) -> deque[float]:
        events = self._events.get(caller)
        if events is None:
            return deque()
        while events and now - events[0] > self.window:
            events.popleft()
        if not events:
            del self._events[caller]
        return events

    def blocked(self, caller: str) -> bool:
        return len(self._recent(caller, time.monotonic())) >= self.limit

    def record(self, caller: str) -> None:
        now = time.monotonic()
        self._recent(caller, now)
        self._events.setdefault(caller, deque()).append(now)
        self._events.move_to_end(caller)
        while len(self._events) > self.max_callers:  # bounded memory under a flood of addresses
            self._events.popitem(last=False)


_KEY_IN_URL = re.compile(r"([?&]key=)[^&\s\"']+")


def redact(text: str) -> str:
    return _KEY_IN_URL.sub(r"\1[redacted]", text)


class RedactKeyFilter(logging.Filter):
    """Keeps ``?key=...`` out of the access log (uvicorn logs the full request path)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        return True


def install_log_redaction() -> None:
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactKeyFilter) for f in logger.filters):
            logger.addFilter(RedactKeyFilter())
