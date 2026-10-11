"""The extraction report reads a directory of fixture files, not only the corpus (S59, S27)."""

from pathlib import Path

from tests.eval.extraction.report import collect_fixtures, render


def test_a_fixture_directory_is_measured_file_by_file(tmp_path: Path) -> None:
    (tmp_path / "clean.txt").write_text("The derivative measures how a function changes.")
    (tmp_path / "scan.md").write_text("T h e d e r i v a t i v e m e a s u r e s")
    (tmp_path / "ignored.bin").write_bytes(b"\x00\x01")

    rows = collect_fixtures(tmp_path)

    assert sorted(r.chunk_id for r in rows) == ["clean.txt", "scan.md"]
    scan = next(r for r in rows if r.chunk_id == "scan.md")
    assert scan.indicators.isolated_letter_ratio > 0.5
    assert "2 chunks" in render(rows) or "of 2 chunks" in render(rows)
