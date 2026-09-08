"""Point the suite at its own database before application settings are read.

``app.core.db`` builds its engine at import time from ``get_settings()``, and
``tests/conftest.py`` imports the FastAPI app — so the redirect has to happen earlier than
any of that. pytest loads the rootdir ``conftest.py`` first, which makes this the one place
it can go. See :mod:`tests.testdb` for why the suite needs a database of its own.
"""

import os

from app.core.config import Settings
from tests.testdb import test_database_url

# Settings() (not get_settings()) resolves .env without populating the settings cache.
os.environ["GURU_DATABASE_URL"] = test_database_url(Settings().database_url)
