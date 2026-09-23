"""Bounded background threads for CPU-heavy work.

`asyncio.to_thread` shares an unbounded-queue pool of several threads, so a burst of
large DOCX parses, password hashes, or token counts could run side by side and
exceed the container's memory limit. Each worker here has exactly one thread and an
admission limit: request-path callers are turned away when the worker is busy, while
internal callers (already bounded by the generation concurrency cap) may wait.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")


class WorkerBusy(RuntimeError):
    """The worker already has its maximum number of running and waiting jobs."""


class BoundedWorker:
    def __init__(self, name: str, *, max_pending: int) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._max_pending = max_pending
        self._pending = 0

    @property
    def pending(self) -> int:
        """Jobs admitted and not yet finished (one running plus any waiting)."""

        return self._pending

    async def run(self, call: Callable[[], T], *, wait: bool = False) -> T:
        """Run ``call`` on the worker thread.

        Without ``wait``, raise `WorkerBusy` instead of queueing past the limit.
        """

        if not wait and self._pending >= self._max_pending:
            raise WorkerBusy
        loop = asyncio.get_running_loop()
        future: Future[T] = self._executor.submit(call)
        self._pending += 1

        def release(_done: Future[T]) -> None:
            try:
                loop.call_soon_threadsafe(self._release)
            except RuntimeError:  # The event loop already closed during shutdown.
                pass

        # Count a job until its thread actually finishes, even if the awaiting
        # request is cancelled first.
        future.add_done_callback(release)
        return await asyncio.wrap_future(future)

    def _release(self) -> None:
        self._pending -= 1

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
