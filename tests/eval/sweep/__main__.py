"""`poe sweep <config.yaml>` — run a live sweep, logging to MLflow. Paid/slow/manual.

Not part of `poe check`/`poe eval`. Requires provider credentials (OpenRouter for frontier
models, a running Ollama for OSS). The `retrieval` suite additionally needs a DB session, which
this CLI does not yet wire (role-selection-v1 is rubric-only); such a cell logs `status=failed`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from app.core.config import get_settings
from app.llm.registry import build_llm_client
from tests.eval.sweep.config import load_sweep_config
from tests.eval.sweep.orchestrate import run_sweep
from tests.eval.sweep.tracking import MLflowTracker


async def _run(config_path: str) -> None:
    config = load_sweep_config(config_path)
    base_client = build_llm_client(get_settings())
    tracker = MLflowTracker(experiment=config.name)
    artifact_dir = Path("mlruns") / "artifacts" / config.name
    await run_sweep(config, base_client, tracker, artifact_dir=artifact_dir)
    print(f"sweep '{config.name}' complete — view with: uv run mlflow ui")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: poe sweep <config.yaml>", file=sys.stderr)
        return 2
    asyncio.run(_run(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
