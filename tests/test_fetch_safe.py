"""Outbound-fetch safety: is_public_address (pure), plus the policy both fetchers share.

Mirrors robots_allows's pure-function precedent — no network needed for the address-safety
predicate. The end-to-end tests drive a stub transport with DNS answers under test control,
so a redirect to an internal address can be exercised without one existing.
"""

from dataclasses import replace

import httpx
import pytest

from app.rag.fetch import (
    LEARNER_POLICY,
    MODEL_POLICY,
    FetchError,
    default_fetch,
    fetch_url,
    is_public_address,
    safe_fetch,
)

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


# --- one policy for learner- and model-supplied URLs -----------------------------------------


@pytest.fixture
def routes(monkeypatch: pytest.MonkeyPatch):
    """Serve canned responses over a stub transport, with DNS answers we control.

    `public` maps hostname -> resolved address, so a redirect target can be made internal
    without touching real DNS or the network.
    """
    responses: dict[str, httpx.Response] = {}
    dns: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        if key.endswith("/robots.txt"):
            return httpx.Response(404)
        if key not in responses:
            return httpx.Response(404)
        return responses[key]

    async def resolve(hostname: str) -> list[str]:
        return [dns.get(hostname, "93.184.216.34")]

    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    monkeypatch.setattr("app.rag.fetch._resolve_ips", resolve)
    return responses, dns


async def test_a_learner_supplied_url_to_an_internal_address_is_refused(routes) -> None:
    """The hole this closes: ingestion took any URL a learner typed, with no address check."""
    _, dns = routes
    dns["metadata.internal"] = "169.254.169.254"
    with pytest.raises(FetchError, match="non-public address"):
        await default_fetch("http://metadata.internal/latest/meta-data/")


async def test_a_redirect_into_an_internal_address_is_refused(routes) -> None:
    """httpx followed redirects itself, so the address check only ever saw the first URL."""
    responses, dns = routes
    responses["https://public.example/start"] = httpx.Response(
        302, headers={"location": "http://metadata.internal/latest/meta-data/"}
    )
    dns["metadata.internal"] = "169.254.169.254"

    with pytest.raises(FetchError, match="non-public address"):
        await default_fetch("https://public.example/start")


async def test_a_public_redirect_is_followed_for_a_learner_and_refused_for_the_model(
    routes,
) -> None:
    responses, _ = routes
    responses["https://public.example/short"] = httpx.Response(
        302, headers={"location": "https://public.example/full"}
    )
    responses["https://public.example/full"] = httpx.Response(
        200, content=b"<p>hello</p>", headers={"content-type": "text/html; charset=utf-8"}
    )

    data, content_type = await default_fetch("https://public.example/short")
    assert data == b"<p>hello</p>" and content_type == "text/html"

    with pytest.raises(FetchError, match="redirect"):
        await safe_fetch("https://public.example/short")


async def test_a_redirect_chain_is_bounded(routes) -> None:
    responses, _ = routes
    for i in range(6):
        responses[f"https://public.example/{i}"] = httpx.Response(
            302, headers={"location": f"https://public.example/{i + 1}"}
        )
    with pytest.raises(FetchError, match="at most 3 redirect"):
        await default_fetch("https://public.example/0")


async def test_an_oversized_body_is_abandoned_rather_than_measured_afterwards(routes) -> None:
    """A cap checked after buffering is not a cap: the bytes have already been paid for."""
    responses, _ = routes
    responses["https://public.example/big"] = httpx.Response(200, content=b"x" * 5_000)

    with pytest.raises(FetchError, match="exceeds 1000 bytes"):
        await fetch_url("https://public.example/big", replace(LEARNER_POLICY, max_bytes=1_000))


async def test_the_model_policy_still_refuses_a_non_public_first_hop(routes) -> None:
    _, dns = routes
    dns["internal.example"] = "127.0.0.1"
    with pytest.raises(FetchError, match="non-public address"):
        await fetch_url("http://internal.example/admin", MODEL_POLICY)
