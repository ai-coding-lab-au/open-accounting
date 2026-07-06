"""HTTP-related helpers.

Two distinct concerns live here:

  1. `safe_filename` — sanitise user-supplied strings before embedding them in
     Content-Disposition (HTTP response headers).

  2. `loopback_only_client` — the project's single sanctioned way to make an
     *outbound* HTTP call. Refuses to talk to anything other than 127.0.0.1
     / [::1] / localhost. This is the code-level guarantee that customer
     accounting data cannot leak to the network: every outbound HTTP call
     in the backend must go through this client, and the client will raise
     before any bytes leave the process if the destination isn't loopback.

     The check is enforced on the resolved URL host inside an httpx event
     hook, not on a raw string prefix, so common SSRF tricks like
     `http://127.0.0.1@evil.com/` cannot bypass it.

     Part 3 (ATO submission drafts) will introduce a separate, dedicated
     client with an explicit allowlist. That client must live in the
     submission subpackage and nowhere else.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

import httpx


_UNSAFE_FILENAME = re.compile(r'[^A-Za-z0-9._-]+')
_UNSAFE_FILENAME_SPACES = re.compile(r'[^A-Za-z0-9 ._-]+')

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


class NetworkPolicyViolation(RuntimeError):
    """Raised when code tries to make an outbound HTTP call to a non-loopback host."""


def safe_filename(
    name: str, *, default: str = "file", max_len: int = 80, allow_spaces: bool = False
) -> str:
    """Return a filename safe to embed in a Content-Disposition header.

    Why: any CR/LF/quote in a user-supplied string would let an attacker inject
    a new HTTP header. We keep only [A-Za-z0-9._-] and collapse anything else
    to an underscore. `allow_spaces` additionally keeps spaces — safe inside a
    quoted filename and used for human-friendly names (e.g. a company name
    prefix) — while still stripping the header-injection characters.
    """
    pattern = _UNSAFE_FILENAME_SPACES if allow_spaces else _UNSAFE_FILENAME
    cleaned = pattern.sub("_", (name or "").strip())
    cleaned = cleaned.strip("._-") or default
    return cleaned[:max_len]


def is_loopback_host(host: str) -> bool:
    """Return True if `host` is a loopback address or name.

    Accepts:
      - "127.0.0.1" and the rest of 127.0.0.0/8
      - "::1"
      - "localhost" / "localhost.localdomain"

    Rejects everything else, including:
      - public IPs and DNS names
      - "0.0.0.0" (wildcard, not loopback — binding != reaching)
      - 169.254.x.x link-local (AWS/Azure metadata endpoint lives there)
    """
    if not host:
        return False
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback


def assert_loopback_url(url: str) -> None:
    """Raise NetworkPolicyViolation unless `url` targets a loopback host.

    Validates the *parsed* host, so authority-confusion bypasses like
    `http://127.0.0.1@evil.com/` (where the real host is evil.com) fail.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not is_loopback_host(host):
        raise NetworkPolicyViolation(
            f"Refusing outbound HTTP to non-loopback host {host!r} "
            f"(url={url!r}). All backend network calls must stay on "
            "127.0.0.1; cloud calls are only permitted from the Part 3 "
            "submission subpackage."
        )


def _enforce_loopback_on_request(request: httpx.Request) -> None:
    assert_loopback_url(str(request.url))


def loopback_only_client(
    *,
    base_url: str = "",
    timeout: float = 600.0,
    **kwargs,
) -> httpx.Client:
    """Return an httpx.Client that refuses non-loopback requests.

    Use this for backend outbound HTTP calls that must stay local-only.
    The check fires on each request via an event hook, so even if a caller
    overrides the URL or passes a full URL into .get()/.post(), it still
    gets validated before the request goes on the wire.

    If `base_url` is provided it is validated up-front so misconfiguration
    fails early instead of on the first request.
    """
    if base_url:
        assert_loopback_url(base_url)

    hooks = kwargs.pop("event_hooks", {}) or {}
    request_hooks = list(hooks.get("request", []))
    request_hooks.append(_enforce_loopback_on_request)
    hooks["request"] = request_hooks

    return httpx.Client(
        base_url=base_url,
        timeout=timeout,
        event_hooks=hooks,
        **kwargs,
    )
