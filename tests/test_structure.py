"""Blocks survive chunking whole, or split where a reader can still use each part (S27)."""

from app.rag import pipeline
from app.rag.adapters.base import ExtractedUnit
from app.rag.chunking import DEFAULT_OVERLAP, DEFAULT_SIZE, _windows, chunk_units, normalize
from app.rag.structure import segment

CAP = 4 * DEFAULT_SIZE


def _chunks(text: str) -> list[ExtractedUnit]:
    return chunk_units([ExtractedUnit(text=text)])


def test_segments_cover_the_text_and_find_each_kind() -> None:
    text = (
        "Intro line.\n"
        "```python\nx = 1\n```\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
        "$$\nE = mc^2\n$$\n"
        "\\begin{align*}\na &= b\n\\end{align*}\n"
        "Outro."
    )
    kinds = [s.kind for s in segment(text)]
    assert kinds == ["prose", "code", "table", "math", "math", "prose"]
    assert "".join(s.text for s in segment(text)) == text


def test_an_unclosed_fence_runs_to_the_end() -> None:
    [prose, code] = segment("before\n```\nnever closed\nstill code")
    assert prose.kind == "prose" and code.kind == "code"
    assert code.text.endswith("still code")
    assert code.close is None


def test_math_inside_a_fence_stays_code() -> None:
    assert [s.kind for s in segment("```\n$$\nnot math\n$$\n```")] == ["code"]


def test_one_line_display_math_does_not_swallow_what_follows() -> None:
    text = "\\[ a^2 + b^2 = c^2 \\]\nThen prose.\n$$ x = 1 $$\nMore prose.\n\\]"
    assert [s.kind for s in segment(text)] == ["math", "prose", "math", "prose"]


def test_a_block_longer_than_a_window_is_one_chunk() -> None:
    code = "```python\n" + "\n".join(f"step_{n}()" for n in range(150)) + "\n```"
    assert DEFAULT_SIZE < len(code) < CAP

    [chunk] = _chunks(code)

    assert chunk.text == code
    assert chunk.locator["structure"] == "code"
    assert "part" not in chunk.locator


def test_a_block_that_fits_a_window_stays_with_its_prose() -> None:
    """A short equation is read with the sentence that introduces it, not as a chunk alone."""
    text = "The energy is given by\n$$\nE = mc^2\n$$\nwhere c is the speed of light."

    [chunk] = _chunks(text)

    assert chunk.text == text
    assert "structure" not in chunk.locator


def test_an_oversize_table_repeats_its_header_in_every_part() -> None:
    header = "| name | value |\n|---|---|"
    rows = "\n".join(f"| row {n} | {n * 7} |" for n in range(400))
    parts = _chunks(f"{header}\n{rows}")

    assert len(parts) > 1
    assert all(p.text.startswith(header) for p in parts)
    assert all(len(p.text) <= CAP for p in parts)
    assert [p.locator["part"] for p in parts] == [
        f"{i}/{len(parts)}" for i in range(1, len(parts) + 1)
    ]
    assert all(p.locator["structure"] == "table" for p in parts)
    assert sum(p.text.count("| row ") for p in parts) == 400, "every row lands in exactly one part"


def test_oversize_code_is_refenced_with_its_language() -> None:
    code = "```python\n" + "\n".join(f"call_number_{n}()" for n in range(400)) + "\n```"
    parts = _chunks(code)

    assert len(parts) > 1
    assert all(p.text.startswith("```python\n") and p.text.endswith("\n```") for p in parts)
    assert all(len(p.text) <= CAP for p in parts)


def test_an_unclosed_oversize_fence_is_not_given_a_closing_fence() -> None:
    code = "```python\n" + "\n".join(f"call_number_{n}()" for n in range(400))
    parts = _chunks(code)

    assert len(parts) > 1
    assert all(p.text.startswith("```python\n") for p in parts)
    assert not any(p.text.endswith("```") for p in parts)


def test_oversize_math_is_rewrapped_in_its_environment() -> None:
    body = "\n".join(f"x_{n} &= y_{n} + z_{n} \\\\" for n in range(400))
    parts = _chunks(f"\\begin{{align}}\n{body}\n\\end{{align}}")

    assert len(parts) > 1
    assert all(
        p.text.startswith("\\begin{align}\n") and p.text.endswith("\n\\end{align}") for p in parts
    )


def test_a_line_longer_than_the_cap_is_cut_but_not_dropped() -> None:
    blob = "A" * (CAP * 2 + 17)
    parts = _chunks(f"```\n{blob}\n```")

    assert all(len(p.text) <= CAP for p in parts)
    assert sum(p.text.count("A") for p in parts) == len(blob)


def test_prose_only_text_chunks_exactly_as_before() -> None:
    prose = "\n\n".join(f"Paragraph {n} says something about cells." * 5 for n in range(40))
    expected = [
        text for _, text in _windows(normalize(prose), size=DEFAULT_SIZE, overlap=DEFAULT_OVERLAP)
    ]

    assert [c.text for c in _chunks(prose)] == expected
    assert all("structure" not in c.locator for c in _chunks(prose))


def test_the_pipeline_version_moved_because_chunk_text_changes() -> None:
    assert pipeline.PIPELINE_VERSION == 2


def test_a_form_feed_is_not_a_line_break() -> None:
    """PDFs emit form feeds; only ``\\n`` ends a line here, as it does for the chunker."""
    text = "| a | b |\x0c| c |\nprose"
    assert [s.kind for s in segment(text)] == ["prose"]
