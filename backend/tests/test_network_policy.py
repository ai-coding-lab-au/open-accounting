"""Tests for the project's no-outbound-network policy.

The intent these tests guard:
    Customer accounting data must never reach a non-loopback host. Every
    outbound HTTP call in the backend goes through utils.http.loopback_only_client,
    which raises NetworkPolicyViolation before any bytes leave the process.

If a future change makes one of these tests fail, the policy has been
weakened — don't "fix" the test, fix the code.
"""

from __future__ import annotations

import pytest

from app.utils.http import (
    NetworkPolicyViolation,
    assert_loopback_url,
    is_loopback_host,
    loopback_only_client,
)


# ---------------------------------------------------------------------------
# is_loopback_host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.2",  # all of 127.0.0.0/8 is loopback
        "127.255.255.254",
        "::1",
        "localhost",
        "LOCALHOST",  # case-insensitive
        "localhost.localdomain",
    ],
)
def test_loopback_hosts_accepted(host):
    assert is_loopback_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "",
        "0.0.0.0",                 # wildcard bind addr is not loopback
        "8.8.8.8",                 # public DNS
        "169.254.169.254",         # AWS/Azure instance metadata
        "192.168.1.1",             # LAN router
        "10.0.0.1",                # private LAN
        "evil.com",
        "api.openai.com",
        "localhost.evil.com",      # not actually localhost
        "127.0.0.1.evil.com",      # not actually 127.0.0.1
    ],
)
def test_non_loopback_hosts_rejected(host):
    assert not is_loopback_host(host)


# ---------------------------------------------------------------------------
# assert_loopback_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/api/generate",
        "http://localhost:8000/anything",
        "https://[::1]:8443/",
    ],
)
def test_loopback_urls_pass(url):
    assert_loopback_url(url)  # should not raise


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1/chat/completions",
        "https://api.anthropic.com/v1/messages",
        # SSRF classic: userinfo confusion — the *real* host is evil.com,
        # not 127.0.0.1. A prefix-string check would let this through; we
        # parse the URL and only accept the resolved hostname.
        "http://127.0.0.1@evil.com/leak",
        "http://localhost@evil.com/leak",
        # Schemeless: urlparse can't extract a hostname, so this fails closed.
        "127.0.0.1:11434/foo",
    ],
)
def test_non_loopback_urls_blocked(url):
    with pytest.raises(NetworkPolicyViolation):
        assert_loopback_url(url)


# ---------------------------------------------------------------------------
# loopback_only_client - local-only outbound HTTP guard.
# ---------------------------------------------------------------------------


def test_client_rejects_non_loopback_base_url():
    # Misconfiguration must fail before the client is even handed back.
    with pytest.raises(NetworkPolicyViolation):
        loopback_only_client(base_url="https://api.openai.com")


def test_client_rejects_per_request_non_loopback_url():
    # Even if base_url is loopback, a caller can't get out by passing a
    # full off-host URL to .get() — the per-request event hook re-validates.
    client = loopback_only_client(base_url="http://127.0.0.1:11434")
    with pytest.raises(NetworkPolicyViolation):
        client.get("http://evil.com/leak")


def test_client_rejects_userinfo_bypass():
    client = loopback_only_client()
    with pytest.raises(NetworkPolicyViolation):
        client.get("http://127.0.0.1@evil.com/leak")
