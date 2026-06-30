"""Weblink fetching for URL sources: robots-aware HTTP GET.

A ``Fetcher`` returns ``(bytes, content_type)`` for a URL; it's a seam so the ingestion
job can be tested with a canned fetcher (no live network). ``default_fetch`` honors
``robots.txt`` and caps response size. Robots parsing is factored into the pure
``robots_allows`` so it's unit-testable without the network.
"""

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
