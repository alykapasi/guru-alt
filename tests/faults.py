"""Faults injected on purpose, at boundaries that really fail (S58).

The suite already covers dependencies that fail *before* they start: a provider whose stream
raises on the first chunk, a queue that refuses every dispatch, a store with nothing in it.
Those are the cheap failures. None of them reaches the state that makes an outage expensive —
half a reply already on the learner's screen, four embedding batches already billed, a walk of
the object store abandoned a thousand keys in with no verdict either way.

So every fault here fires **mid-operation**, after real work has been done and real money
spent, and every one of them records that it fired. A fault that silently failed to inject
reports as a pass, which is the same defect as a mutation that did not apply.
"""

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from app.llm.providers import FakeProvider
from app.llm.types import ChatChunk, ChatMessage, EmbedResult, ToolDef, Usage
from app.storage.base import DEFAULT_CONTENT_TYPE, BlobStore


class InjectedFault(RuntimeError):
    """Raised only by this module, so a test can tell its own fault from a real defect."""


class DyingStream(FakeProvider):
    """Delivers ``after`` tokens and then loses the connection.

    Pointedly not the same fault as a stream that raises on its first chunk. By the time this
    one fires the tokens have already been yielded to the caller and are on their way to the
    learner's screen, so the question it asks is what the server does with the half it holds —
    which the pre-first-token case cannot ask, because there is no half.
    """

    def __init__(self, reply: str, *, after: int) -> None:
        super().__init__(reply)
        self._after = after
        self.delivered = 0

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> AsyncIterator[ChatChunk]:
        for i, word in enumerate(self._reply.split()):
            if i == self._after:
                raise InjectedFault(f"the connection dropped after {self.delivered} tokens")
            self.delivered += 1
            yield ChatChunk(text=word if i == 0 else f" {word}")
        raise InjectedFault("the reply was shorter than the fault point; raise `after`")


class FailingEmbedBatch(FakeProvider):
    """Embeds normally except for the ``on_call``-th batch, which fails.

    The batches around it still run and are still charged for, which is the entire point: a
    provider that refuses the first call costs nothing, and the failure worth reasoning about
    is the one that happens after the money has gone.
    """

    def __init__(self, *, on_call: int) -> None:
        super().__init__()
        self._on_call = on_call
        self.calls = 0
        self.fired = 0
        self.billed = Usage()

    async def embed(self, *, model: str, texts: Sequence[str]) -> EmbedResult:
        self.calls += 1
        if self.calls == self._on_call:
            self.fired += 1
            raise InjectedFault(f"the embedding provider failed on batch {self.calls}")
        result = await super().embed(model=model, texts=texts)
        self.billed = Usage(
            input_tokens=self.billed.input_tokens + result.usage.input_tokens,
            output_tokens=self.billed.output_tokens + result.usage.output_tokens,
        )
        return result


class FaultyBlobStore:
    """A store that works until the ``on_call``-th call to ``method``, then does not.

    It wraps a real store rather than replacing one, so everything on either side of the fault
    is genuine: the bytes really are there, the calls before and after really do read them, and
    exactly one call fails. A stub that failed everything would prove only that the caller
    propagates an exception.
    """

    def __init__(self, inner: BlobStore, *, method: str, on_call: int = 1) -> None:
        self._inner = inner
        self._method = method
        self._on_call = on_call
        self._seen = 0
        self.fired = 0

    def _maybe_fail(self, method: str) -> None:
        if method != self._method:
            return
        self._seen += 1
        if self._seen == self._on_call:
            self.fired += 1
            raise InjectedFault(f"the object store failed on {method} call {self._seen}")

    async def put(self, key: str, data: bytes, *, content_type: str = DEFAULT_CONTENT_TYPE) -> str:
        self._maybe_fail("put")
        return await self._inner.put(key, data, content_type=content_type)

    async def get(self, key: str) -> bytes:
        self._maybe_fail("get")
        return await self._inner.get(key)

    async def upload(self, key: str, src: Path, *, content_type: str = DEFAULT_CONTENT_TYPE) -> str:
        self._maybe_fail("upload")
        return await self._inner.upload(key, src, content_type=content_type)

    async def download(self, key: str, dest: Path) -> None:
        self._maybe_fail("download")
        await self._inner.download(key, dest)

    async def delete(self, key: str) -> None:
        self._maybe_fail("delete")
        await self._inner.delete(key)

    async def exists(self, key: str) -> bool:
        self._maybe_fail("exists")
        return await self._inner.exists(key)

    async def presigned_url(self, key: str, *, expires_in: int = 3600) -> str:
        self._maybe_fail("presigned_url")
        return await self._inner.presigned_url(key, expires_in=expires_in)
