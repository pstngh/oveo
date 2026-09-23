"""Bound sign-in work: password hashing, per-account ordering, and failure rates."""

from __future__ import annotations

import asyncio
import time
import weakref
from collections import Counter, OrderedDict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial

from oveo.auth import verify_password
from oveo.workers import BoundedWorker, WorkerBusy

# One Argon2 verification uses 64 MiB and ~0.15 s of CPU on the production vCPU, so
# only one runs at a time and only a few more may wait; the rest are refused.
_MAX_PENDING_VERIFICATIONS = 4
_MAX_WAITING_PER_ACCOUNT = 3
_MAX_TRACKED_CLIENTS = 1_024


class LoginBusy(Exception):
    """Too many sign-in attempts are already being checked."""


class LoginGuard:
    def __init__(
        self,
        *,
        window_seconds: float,
        client_failure_limit: int,
        global_failure_limit: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._worker = BoundedWorker("oveo-argon2", max_pending=_MAX_PENDING_VERIFICATIONS)
        self._window = window_seconds
        self._client_limit = client_failure_limit
        self._global_limit = global_failure_limit
        self._clock = clock
        self._global: deque[float] = deque()
        self._clients: OrderedDict[str, deque[float]] = OrderedDict()
        self._accounts: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._waiting: Counter[str] = Counter()

    def retry_after(self, client: str) -> int | None:
        """Seconds until this client may try again, or None if it may try now.

        Checked before any password hashing, so a burst of failures from one client,
        or from everyone, stops costing CPU.
        """

        now = self._clock()
        self._expire(self._global, now)
        waits: list[float] = []
        if len(self._global) >= self._global_limit:
            waits.append(self._global[0] + self._window - now)
        failures = self._clients.get(client)
        if failures is not None:
            self._expire(failures, now)
            if len(failures) >= self._client_limit:
                waits.append(failures[0] + self._window - now)
        return max(1, int(max(waits)) + 1) if waits else None

    def record_failure(self, client: str) -> None:
        now = self._clock()
        self._global.append(now)
        failures = self._clients.pop(client, None) or deque()
        failures.append(now)
        self._clients[client] = failures
        while len(self._clients) > _MAX_TRACKED_CLIENTS:
            self._clients.popitem(last=False)

    def _expire(self, failures: deque[float], now: float) -> None:
        while failures and failures[0] <= now - self._window:
            failures.popleft()

    @asynccontextmanager
    async def account(self, username: str) -> AsyncIterator[None]:
        """Serialize attempts for one account so its failure counter never races.

        Concurrent guesses used to read the same counter and all be evaluated before the
        lock engaged. Each attempt now re-reads the account after the previous one wrote.
        """

        if self._waiting[username] >= _MAX_WAITING_PER_ACCOUNT:
            raise LoginBusy
        lock = self._accounts.get(username)
        if lock is None:
            lock = asyncio.Lock()
            self._accounts[username] = lock
        self._waiting[username] += 1
        try:
            async with lock:
                yield
        finally:
            self._waiting[username] -= 1
            if self._waiting[username] <= 0:
                del self._waiting[username]

    async def verify(self, password_hash: str, password: str) -> bool:
        try:
            return await self._worker.run(partial(verify_password, password_hash, password))
        except WorkerBusy as exc:
            raise LoginBusy from exc

    def close(self) -> None:
        self._worker.close()
