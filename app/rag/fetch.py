"""Outbound HTTP: one policy, applied to every URL the product fetches.

A ``Fetcher`` returns ``(bytes, content_type)`` for a URL; it's a seam so callers can be
tested with a canned fetcher (no live network).

There used to be two fetchers with two different postures: ``safe_fetch`` refused non-public
addresses for **model**-chosen URLs, while ``default_fetch`` — the one that fetches URLs a
**learner** types into ingestion — checked nothing and followed redirects wherever they led.
A learner-supplied URL is not a trusted URL, and the address check is not what distinguishes
the two callers, so both now run the same checks: scheme, host, and every resolved address
must be public, on the original URL **and on every redirect hop**. What differs is only how
far a redirect chain may run — see :class:`FetchPolicy`.

Response bodies (including ``robots.txt``) are read incrementally and abandoned the moment
they exceed their cap; the size limit used to be checked after the whole body was already in
memory, which is not a limit.

Address safety and robots parsing are each a pure function (``is_public_address``,
``robots_allows``) so both are unit-testable without network.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import structlog

log = structlog.get_logger(__name__)

USER_AGENT = "GuruBot/0.1 (+https://guru.example/bot)"
MAX_BYTES = 5_000_000
ROBOTS_MAX_BYTES = 512_000  # a robots.txt this large is not a robots.txt
TIMEOUT = 10.0

FetchResult = tuple[bytes, str]
Fetcher = Callable[[str], Awaitable[FetchResult]]


@dataclass(frozen=True)
class FetchPolicy:
    """How far one caller's fetch may go. The safety checks are not negotiable per caller."""

    max_redirects: int
    max_bytes: int = MAX_BYTES
    timeout: float = TIMEOUT


# A learner pasting a link routinely pastes a shortened or canonicalising one, so a bounded
# chain is followed — with the address check repeated at every hop, which is the part that was
# missing when redirects were followed by httpx itself.
LEARNER_POLICY = FetchPolicy(max_redirects=3)

# The model chooses this URL from conversation text that may itself have come from an untrusted
# page, so no hop we have not individually seen and approved is taken.
MODEL_POLICY = FetchPolicy(max_redirects=0)


class FetchError(RuntimeError):
    """A URL could not be fetched."""


class RobotsDisallowed(FetchError):
    """robots.txt forbids fetching this URL with our user agent."""


class FetchTransportError(FetchError):
    """The request itself failed — connection refused, reset, timed out.

    Split from ``FetchError`` because every *other* thing that class reports is a permanent
    fact about the URL: robots forbids it, the scheme is wrong, it resolves somewhere private,
    it is too big. Those are worth reporting to the learner and never worth retrying. This one
    is a statement about the moment, and retrying it is exactly right. Ingestion's retry
    classifier depends on being able to tell the two apart.
    """


def robots_allows(robots_txt: str, user_agent: str, url: str) -> bool:
    """Whether ``robots_txt`` permits ``user_agent`` to fetch ``url`` (pure)."""
    parser = RobotFileParser()
    parser.parse(robots_txt.splitlines())
    return parser.can_fetch(user_agent, url)


def is_public_address(ip: str) -> bool:
    """Whether ``ip`` is globally routable and non-multicast — safe to connect to (pure).

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


async def _require_safe_destination(url: str) -> None:
    """Refuse anything but http(s) to a host whose every resolved address is public."""
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


async def _read_bounded(response: httpx.Response, limit: int, url: str) -> bytes:
    """Read a streaming response, giving up as soon as it exceeds ``limit``.

    The point of a size cap is to not receive the bytes. Buffering the whole body and
    measuring it afterwards let an oversized (or endless) response cost the memory first.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise FetchError(f"{url} exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _robots_ok(client: httpx.AsyncClient, url: str) -> bool:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        async with client.stream(
            "GET", robots_url, headers={"User-Agent": USER_AGENT}, timeout=5.0
        ) as resp:
            if resp.status_code >= 400:
                return True  # no robots.txt → allowed
            body = await _read_bounded(resp, ROBOTS_MAX_BYTES, robots_url)
    except httpx.HTTPError:
        return True  # robots unreachable → allowed by convention
    except FetchError:
        log.warning("fetch.robots_too_large", url=robots_url)
        return True  # an unreadable robots.txt is treated as absent, as above
    return robots_allows(body.decode("utf-8", errors="replace"), USER_AGENT, url)


async def fetch_url(url: str, policy: FetchPolicy) -> FetchResult:
    """Fetch ``url`` under ``policy``, validating the destination at every hop.

    Redirects are followed by this function rather than by httpx, because httpx following
    them means the address check applies only to the first URL — a public host redirecting to
    ``169.254.169.254`` walks straight past it.

    Known accepted gap: a DNS-rebinding window remains between the address check and httpx's
    own resolution on connect. Closing it needs connect-time pinning of the verified address,
    which is a transport-level change, not a check-order one.
    """
    async with httpx.AsyncClient(follow_redirects=False, timeout=policy.timeout) as client:
        for hop in range(policy.max_redirects + 1):
            await _require_safe_destination(url)
            if not await _robots_ok(client, url):
                raise RobotsDisallowed(f"robots.txt disallows {url}")
            try:
                async with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location", "")
                        if not location:
                            raise FetchError(f"{url} redirects with no location")
                        if hop >= policy.max_redirects:
                            raise FetchError(
                                f"{url} redirects to {location}; at most "
                                f"{policy.max_redirects} redirect(s) are followed"
                            )
                        url = str(resp.url.join(location))
                        continue
                    resp.raise_for_status()
                    data = await _read_bounded(resp, policy.max_bytes, url)
                    content_type = resp.headers.get("content-type", "text/html")
            except httpx.HTTPError as exc:
                raise FetchTransportError(f"failed to fetch {url}: {exc}") from exc
            return data, content_type.split(";", 1)[0].strip() or "text/html"
    raise FetchError(f"too many redirects fetching {url}")  # pragma: no cover - unreachable


async def default_fetch(url: str) -> FetchResult:
    """Fetch a URL a learner supplied (weblink ingestion)."""
    return await fetch_url(url, LEARNER_POLICY)


async def safe_fetch(url: str) -> FetchResult:
    """Fetch a URL the model chose (the ``fetch_webpage`` tool)."""
    return await fetch_url(url, MODEL_POLICY)
