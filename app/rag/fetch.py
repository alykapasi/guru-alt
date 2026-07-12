"""Weblink fetching: robots-aware HTTP GET, plus a hardened variant for model-controlled URLs.

A ``Fetcher`` returns ``(bytes, content_type)`` for a URL; it's a seam so callers can be
tested with a canned fetcher (no live network). ``default_fetch`` (learner-initiated, via URL
ingestion) honors ``robots.txt`` and caps response size. ``safe_fetch`` (model-controlled, via
the ``fetch_webpage`` tool) additionally refuses non-public addresses (SSRF hardening) and
does not follow redirects. Robots parsing and address-safety are each factored into a pure
function (``robots_allows``, ``is_public_address``) so both are unit-testable without network.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

USER_AGENT = "GuruBot/0.1 (+https://guru.example/bot)"
MAX_BYTES = 5_000_000
TIMEOUT = 10.0

FetchResult = tuple[bytes, str]
Fetcher = Callable[[str], Awaitable[FetchResult]]


class FetchError(RuntimeError):
    """A URL could not be fetched."""


class RobotsDisallowed(FetchError):
    """robots.txt forbids fetching this URL with our user agent."""


def robots_allows(robots_txt: str, user_agent: str, url: str) -> bool:
    """Whether ``robots_txt`` permits ``user_agent`` to fetch ``url`` (pure)."""
    parser = RobotFileParser()
    parser.parse(robots_txt.splitlines())
    return parser.can_fetch(user_agent, url)


async def default_fetch(url: str) -> FetchResult:
    """Fetch ``url`` if robots allows, returning its bytes + content type."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT) as client:
        if not await _robots_ok(client, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        try:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to fetch {url}: {exc}") from exc
        data = resp.content
        if len(data) > MAX_BYTES:
            raise FetchError(f"{url} exceeds {MAX_BYTES} bytes")
        content_type = resp.headers.get("content-type", "text/html").split(";", 1)[0].strip()
        return data, content_type or "text/html"


async def _robots_ok(client: httpx.AsyncClient, url: str) -> bool:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        resp = await client.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=5.0)
    except httpx.HTTPError:
        return True  # robots unreachable → allowed by convention
    if resp.status_code >= 400:
        return True  # no robots.txt → allowed
    return robots_allows(resp.text, USER_AGENT, url)


def is_public_address(ip: str) -> bool:
    """Whether ``ip`` is globally routable and non-multicast — safe for a model-controlled
    fetch to connect to (pure, no I/O).

    ``.is_global`` alone excludes private/loopback/link-local/reserved/unspecified *and*
    RFC 6598 CGNAT space (``100.64.0.0/10``) in one check — enumerating ``.is_private`` /
    ``.is_loopback`` / etc. individually misses CGNAT (none of those properties are true for
    it, but ``.is_global`` is correctly ``False``). Multicast is checked separately since
    ``.is_global`` is ``True`` for it. IPv4-mapped IPv6 (``::ffff:127.0.0.1``) is handled
    correctly via ``ipaddress``'s own normalization.
    """
    addr = ipaddress.ip_address(ip)
    return addr.is_global and not addr.is_multicast


async def _resolve_ips(hostname: str) -> list[str]:
    """DNS-resolve ``hostname`` to its address literals, off the event loop."""
    infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def safe_fetch(url: str) -> FetchResult:
    """Like :func:`default_fetch`, but hardened for **model-controlled** URLs.

    The model decides which URL to fetch based on conversation content that may include text
    retrieved from untrusted sources (an ingested page, a search hit) — a categorically
    higher-risk trust boundary than ``default_fetch``'s learner-initiated ingestion path.
    Only ``http``/``https`` is allowed; every DNS-resolved address for the host must be
    public (blocks SSRF against internal services and cloud-metadata endpoints, e.g.
    ``169.254.169.254``); redirects are not followed (a redirect to an internal address would
    bypass the pre-connect check, and per-hop revalidation is deliberately not built for v1).

    Known accepted gaps (documented, not fixed): a DNS-rebinding TOCTOU window between the
    address check below and httpx's own (separate) resolution on connect — for both the
    robots.txt request and the main request. And this only blocks *internal* targets — it
    does not stop a compromised page's content from directing the model to fetch a *public*
    attacker-controlled URL (indirect-prompt-injection exfiltration is a different, unmitigated
    risk category from SSRF).
    """
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise FetchError(f"invalid URL {url!r}: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise FetchError(f"unsupported scheme for {url!r}: only http/https are allowed")
    if not parsed.host:
        raise FetchError(f"no hostname in {url!r}")

    try:
        addresses = await _resolve_ips(parsed.host)
    except socket.gaierror as exc:
        raise FetchError(f"could not resolve {parsed.host}: {exc}") from exc
    if not addresses or not all(is_public_address(ip) for ip in addresses):
        raise FetchError(f"{url} resolves to a non-public address; refusing to fetch")

    async with httpx.AsyncClient(follow_redirects=False, timeout=TIMEOUT) as client:
        if not await _robots_ok(client, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        try:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError as exc:
            raise FetchError(f"failed to fetch {url}: {exc}") from exc
        if resp.is_redirect:
            location = resp.headers.get("location", "?")
            raise FetchError(f"{url} redirects to {location}; safe_fetch does not follow redirects")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise FetchError(f"failed to fetch {url}: {exc}") from exc
        data = resp.content
        if len(data) > MAX_BYTES:
            raise FetchError(f"{url} exceeds {MAX_BYTES} bytes")
        content_type = resp.headers.get("content-type", "text/html").split(";", 1)[0].strip()
        return data, content_type or "text/html"
