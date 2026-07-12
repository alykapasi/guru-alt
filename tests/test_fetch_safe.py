"""SSRF hardening for model-controlled URL fetches: is_public_address (pure) + safe_fetch.

Mirrors robots_allows's pure-function precedent — no network needed for the address-safety
predicate. default_fetch itself has zero direct tests anywhere in this codebase (only exercised
indirectly via an injected Fetcher at the ingestion layer, see test_weblink.py); safe_fetch gets
one extra end-to-end wiring test beyond that same precedent since this is the security-critical
path.
"""

import pytest

from app.rag.fetch import FetchError, is_public_address, safe_fetch

# --- is_public_address --------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "8.8.8.8",
        "2001:4860:4860::8888",
    ],
)
def test_is_public_address_allows_ordinary_public_addresses(ip: str) -> None:
    assert is_public_address(ip) is True


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "::1",  # IPv6 loopback
        "10.0.0.5",  # RFC1918 private
        "192.168.1.1",  # RFC1918 private
        "169.254.169.254",  # link-local — the cloud-metadata endpoint
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "100.64.0.1",  # RFC 6598 CGNAT — missed by naive is_private/is_loopback/etc. enumeration
        "fe80::1",  # IPv6 link-local
        "fc00::1",  # IPv6 unique local (private)
        "::ffff:127.0.0.1",  # IPv4-mapped IPv6 loopback
    ],
)
def test_is_public_address_blocks_internal_and_reserved_addresses(ip: str) -> None:
    assert is_public_address(ip) is False


# --- safe_fetch: fails before any I/O ------------------------------------------------------


async def test_safe_fetch_rejects_non_http_scheme() -> None:
    with pytest.raises(FetchError):
        await safe_fetch("ftp://example.com/file")


async def test_safe_fetch_rejects_hostless_url() -> None:
    with pytest.raises(FetchError):
        await safe_fetch("http:///path")


async def test_safe_fetch_rejects_invalid_url() -> None:
    with pytest.raises(FetchError):
        await safe_fetch("not a url \x00")


# --- safe_fetch: end-to-end address-check wiring --------------------------------------------


async def test_safe_fetch_rejects_a_resolved_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _resolves_to_loopback(hostname: str) -> list[str]:
        return ["127.0.0.1"]

    monkeypatch.setattr("app.rag.fetch._resolve_ips", _resolves_to_loopback)

    with pytest.raises(FetchError):
        await safe_fetch("http://internal.example/admin")
