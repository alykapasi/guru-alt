"""Guru Alt API application.

Runtime type-checking: outside production we install beartype's import hook so every
first-party function in this package is checked against its annotations at call time.
This must run before any submodule is imported, so it lives at the top of the package.
The guard reads the raw env var (config isn't importable yet at this point).
"""

import os

if os.getenv("GURU_ENV", "dev").lower() != "prod":
    from beartype.claw import beartype_this_package

    beartype_this_package()
