"""
Event loop utilities for handling asyncio locks across different contexts.

This module provides utilities to ensure asyncio locks work correctly
across different event loop contexts, particularly important for HTTP
transport where each request might have its own event loop.
"""

import asyncio
import threading
from typing import Any
from weakref import WeakKeyDictionary


class EventLoopLockManager:
    """
    Manages asyncio locks across different event loops.

    This class ensures that locks are created in the correct event loop
    context, preventing "bound to a different event loop" errors.
    """

    def __init__(self) -> None:
        """Initialize the lock manager."""
        # Use weak references to avoid keeping event loops alive
        self._locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            WeakKeyDictionary()
        )
        self._thread_lock = threading.Lock()

    def get_lock(self) -> asyncio.Lock:
        """
        Get or create a lock for the current event loop.

        Returns
        -------
        asyncio.Lock
            A lock bound to the current event loop
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop, create a new lock that will bind when used
            return asyncio.Lock()

        with self._thread_lock:
            if loop not in self._locks:
                self._locks[loop] = asyncio.Lock()
            return self._locks[loop]

    def clear(self) -> None:
        """Clear all locks (useful for testing)."""
        with self._thread_lock:
            self._locks.clear()


class CrossLoopLock:
    """Async context manager providing mutual exclusion across BOTH
    event loops and threads, backed by a process-wide ``threading.Lock``.

    :class:`EventLoopLockManager` above only serializes callers on the
    SAME event loop — each loop gets its OWN ``asyncio.Lock`` instance
    (keyed by loop in a ``WeakKeyDictionary``), so two concurrent
    callers on DIFFERENT loops (e.g. multiple uvicorn workers in the
    same process under HTTP transport, each running its own asyncio
    loop) see no contention at all and race straight through a critical
    section meant to be exclusive — confirmed as a real bug and fixed
    this same way for ``create_project``'s own serialization lock (see
    ``src/marcus_mcp/tools/nlp.py``'s ``_create_project_serialization_lock``,
    Codex P1 on PR #613); this generalizes that fix for reuse (see
    ``MarcusServer.assignment_lock``, the same race class for task
    double-assignment).

    Acquisition polls ``threading.Lock.acquire(blocking=False)`` and
    yields to the event loop between attempts — a real blocking
    ``acquire()`` would freeze the whole event loop while waiting.
    """

    def __init__(self, poll_interval: float = 0.05) -> None:
        """Initialize with a fresh, unlocked ``threading.Lock``.

        Parameters
        ----------
        poll_interval : float
            Seconds to sleep between non-blocking acquire attempts.
        """
        self._lock = threading.Lock()
        self._poll_interval = poll_interval

    async def acquire(self) -> bool:
        """Acquire the lock, yielding to the event loop while waiting.

        Exposed as a direct method (not just ``async with``) so code
        written against ``asyncio.Lock``'s interface — e.g. anything
        that checks ``hasattr(lock, "acquire")`` — still works.

        Returns
        -------
        bool
            Always ``True`` once acquired (matches ``asyncio.Lock
            .acquire()``'s return contract).
        """
        while not self._lock.acquire(blocking=False):
            await asyncio.sleep(self._poll_interval)
        return True

    def release(self) -> None:
        """Release the lock."""
        self._lock.release()

    async def __aenter__(self) -> "CrossLoopLock":
        """Acquire the lock, yielding to the event loop while waiting."""
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Release the lock."""
        self.release()


class ThreadLocalLockManager:
    """
    Thread-local lock manager for simpler cases.

    Each thread gets its own lock, avoiding event loop binding issues.
    """

    def __init__(self) -> None:
        """Initialize the thread-local storage."""
        self._local = threading.local()

    def get_lock(self) -> asyncio.Lock:
        """
        Get or create a lock for the current thread.

        Returns
        -------
        asyncio.Lock
            A lock for the current thread
        """
        if not hasattr(self._local, "lock"):
            self._local.lock = asyncio.Lock()
        lock: asyncio.Lock = self._local.lock
        return lock


def create_event_loop_safe_lock() -> asyncio.Lock:
    """
    Create a lock that's safe to use across event loops.

    This is a simple factory function that creates a new lock
    in the current event loop context.

    Returns
    -------
    asyncio.Lock
        A new lock bound to the current event loop
    """
    return asyncio.Lock()
