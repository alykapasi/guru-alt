"""Writing and reading raw checkpoints through whichever saver is current."""

import uuid

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata

from app.agent import checkpointing


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


async def put_checkpoint(thread_id: str) -> None:
    checkpoint: Checkpoint = {
        "v": 1,
        "id": str(uuid.uuid4()),
        "ts": "",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": None,
    }
    metadata: CheckpointMetadata = {"source": "update", "step": 1, "parents": {}}
    await checkpointing.checkpointer().aput(_config(thread_id), checkpoint, metadata, {})


async def has_checkpoint(thread_id: str) -> bool:
    return bool([c async for c in checkpointing.checkpointer().alist(_config(thread_id))])
