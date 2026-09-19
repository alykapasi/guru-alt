"""Dormant web helpers, uploaded HTML extraction, and legacy link input validation."""

from pathlib import Path

from httpx import AsyncClient

from app.rag.adapters import ExtractContext
from app.rag.adapters.html import HtmlAdapter
from app.rag.fetch import robots_allows

API = "/api/v1"
URL = "https://example.com/cell"

_HTML = b"""<html><head><title>Cells</title></head><body>
<nav>Home About Contact</nav>
<article>
<h1>The Cell</h1>
<p>The cell is the basic structural and functional unit of all living organisms. Cells
are often called the building blocks of life and were first described in the 1600s.</p>
<p>The mitochondrion is the powerhouse of the cell, generating most of the cell's supply
of ATP through the process of cellular respiration.</p>
</article>
<footer>Copyright 2026 Example Corp</footer>
</body></html>"""


# --- robots (pure) ----------------------------------------------------------


def test_robots_allows_permits_unlisted_path() -> None:
    txt = "User-agent: *\nDisallow: /private/"
    assert robots_allows(txt, "GuruBot", "https://x.com/public/page")


def test_robots_allows_blocks_disallowed_path() -> None:
    assert not robots_allows("User-agent: *\nDisallow: /", "GuruBot", "https://x.com/anything")


# --- HTML extraction --------------------------------------------------------


async def test_html_adapter_extracts_main_content(tmp_path: Path) -> None:
    page = tmp_path / "page.html"
    page.write_bytes(_HTML)
    units = await HtmlAdapter().extract(page, meta={"url": URL}, ctx=ExtractContext())
    assert len(units) == 1
    assert units[0].locator == {"url": URL}
    assert "mitochondrion" in units[0].text.lower()
    assert "About Contact" not in units[0].text  # nav stripped


async def test_html_adapter_empty_on_no_content(tmp_path: Path) -> None:
    empty = tmp_path / "empty.html"
    empty.write_bytes(b"<html><body></body></html>")
    assert await HtmlAdapter().extract(empty, meta={}, ctx=ExtractContext()) == []


# URL creation and queued-job refusals are covered in test_v0_web_policy.py.


async def test_link_endpoint_rejects_bad_url(api_client: AsyncClient) -> None:
    r = await api_client.post(f"{API}/sources/link", json={"url": "not-a-url"})
    assert r.status_code == 422
