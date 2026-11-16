"""A migration describes one schema, the same everywhere (review, 2026-11-16).

Reading runtime settings inside a migration makes the same revision build a different schema
in a different environment — ``staged_chunks.embedding`` once took its dimension from
``GURU_EMBED_DIM``, while every other vector column is fixed at 768. Values a migration needs
are written down as they stood at that revision.
"""

from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "db" / "migrations" / "versions"

# Reads settings for data, not schema: it labels existing vectors with the model that wrote
# them, through a server default it drops straight after. A fixed value there would mislabel
# every environment running another model.
ALLOWED = {"0025_embedding_space.py"}


def test_no_migration_reads_runtime_settings() -> None:
    readers = sorted(
        path.name
        for path in VERSIONS.glob("*.py")
        if "get_settings" in path.read_text() and path.name not in ALLOWED
    )
    assert readers == []
