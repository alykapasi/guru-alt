"""`--fresh` drops a database, so it must only ever reach the browser journeys' one (S58)."""

import pytest

from tests.testdb import E2E_SUFFIX, refuse_unless_e2e

BASE = "postgresql+asyncpg://u:p@localhost:5433/guru"


def test_the_e2e_database_may_be_recreated() -> None:
    assert refuse_unless_e2e(f"{BASE}{E2E_SUFFIX}") == f"guru{E2E_SUFFIX}"


@pytest.mark.parametrize("name", ["guru", "guru_test", "guru_perf", "guru_e2e_old", "_e2e"])
def test_any_other_database_is_refused_before_anything_is_dropped(name: str) -> None:
    with pytest.raises(ValueError, match="refusing to drop"):
        refuse_unless_e2e(f"postgresql+asyncpg://u:p@localhost:5433/{name}")


def test_a_quoted_name_is_refused() -> None:
    with pytest.raises(ValueError):
        refuse_unless_e2e('postgresql+asyncpg://u:p@localhost:5433/x"_e2e')
