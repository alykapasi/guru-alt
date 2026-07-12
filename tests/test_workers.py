"""The taskiq broker seam: decorated tasks must stay real, dispatchable task objects.

Regression coverage for a bug where beartype's package-wide import hook
(``beartype_this_package`` in ``app/__init__.py``) silently collapsed ``@broker.task``-decorated
functions down to plain functions missing ``.kiq()`` — because beartype ended up decorating the
``AsyncTaskiqDecoratedTask`` instance itself when ``@broker.task`` sat directly in the same
decorator stack as the function definition. Fixed by applying ``broker.task(...)`` as a plain
call in ``app/workers/tasks.py`` rather than decorator syntax, which beartype's claw hook never
sees (it only rewrites ``def``/``async def`` nodes, not assignment statements). Every test in
this suite runs with beartype's import hook active (only ``GURU_ENV=prod`` disables it), so this
reproduces the failure mode without needing a live server.
"""

from app.workers.tasks import ingest_source_task, memory_write_back_task


def test_ingest_source_task_is_a_dispatchable_taskiq_task() -> None:
    assert hasattr(ingest_source_task, "kiq")


def test_memory_write_back_task_is_a_dispatchable_taskiq_task() -> None:
    assert hasattr(memory_write_back_task, "kiq")
