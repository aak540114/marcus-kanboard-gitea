"""
Unit tests for src/core/event_loop_utils.py's CrossLoopLock.

Regression coverage for a real bug: EventLoopLockManager hands out a
SEPARATE asyncio.Lock per event loop, so two concurrent callers on
DIFFERENT loops (e.g. multiple uvicorn workers in one process under HTTP
transport) see no contention at all — defeating the whole point of using
a lock to serialize a critical section. CrossLoopLock fixes this by
backing the lock with a process-wide threading.Lock instead, polled
non-blockingly so the event loop isn't frozen while waiting. This is the
exact fix already applied to create_project's own serialization lock
(src/marcus_mcp/tools/nlp.py, Codex P1 on PR #613), generalized here for
reuse by MarcusServer.assignment_lock.
"""

import asyncio
import threading
import time

import pytest

from src.core.event_loop_utils import CrossLoopLock, EventLoopLockManager


class TestCrossLoopLockBasicBehavior:
    @pytest.mark.asyncio
    async def test_async_with_acquires_and_releases(self):
        lock = CrossLoopLock(poll_interval=0.01)
        async with lock:
            assert lock._lock.locked()
        assert not lock._lock.locked()

    @pytest.mark.asyncio
    async def test_direct_acquire_and_release(self):
        lock = CrossLoopLock(poll_interval=0.01)
        acquired = await lock.acquire()
        assert acquired is True
        assert lock._lock.locked()
        lock.release()
        assert not lock._lock.locked()

    @pytest.mark.asyncio
    async def test_second_acquire_waits_for_release(self):
        """A second acquire attempt must block (via polling) until the
        first holder releases — proving this is a REAL mutex, not a
        no-op."""
        lock = CrossLoopLock(poll_interval=0.01)
        order = []

        async def holder():
            async with lock:
                order.append("holder-acquired")
                await asyncio.sleep(0.05)
                order.append("holder-releasing")

        async def waiter():
            await asyncio.sleep(0.01)  # ensure holder acquires first
            async with lock:
                order.append("waiter-acquired")

        await asyncio.gather(holder(), waiter())

        assert order == [
            "holder-acquired",
            "holder-releasing",
            "waiter-acquired",
        ]


class TestCrossLoopLockSerializesAcrossRealEventLoops:
    """The actual bug this class fixes: two callers on genuinely
    DIFFERENT event loops (not just different coroutines on the same
    loop) must still serialize. Reproduced with real OS threads, each
    running its own asyncio event loop via asyncio.run() — exactly the
    multi-uvicorn-worker shape described in CrossLoopLock's docstring.
    """

    def test_shared_lock_serializes_two_separate_event_loops(self):
        lock = CrossLoopLock(poll_interval=0.005)
        events = []
        events_guard = threading.Lock()
        barrier = threading.Barrier(2)

        def run_in_own_loop(label: str, hold_seconds: float) -> None:
            async def body():
                barrier.wait()  # start both threads' loops at ~the same time
                async with lock:
                    with events_guard:
                        events.append(f"{label}-acquired")
                    time.sleep(hold_seconds)  # hold the lock
                    with events_guard:
                        events.append(f"{label}-released")

            asyncio.run(body())

        t1 = threading.Thread(target=run_in_own_loop, args=("A", 0.1))
        t2 = threading.Thread(target=run_in_own_loop, args=("B", 0.0))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Whichever thread got in first, the OTHER's acquire must not
        # interleave with it — no "A-acquired, B-acquired, ..." pattern.
        assert events[0].endswith("-acquired")
        assert events[1].endswith("-released")
        assert events[0][0] == events[1][0]  # same thread acquired+released first
        assert events[2].endswith("-acquired")
        assert events[3].endswith("-released")

    def test_demonstrates_the_bug_eventlooplockmanager_has(self):
        """Contrast case, proving the bug CrossLoopLock fixes is real:
        with a plain EventLoopLockManager-vended asyncio.Lock, two
        different event loops get two DIFFERENT Lock objects and do NOT
        serialize — both threads' critical sections overlap."""
        mgr = EventLoopLockManager()
        events = []
        events_guard = threading.Lock()
        barrier = threading.Barrier(2)
        both_inside = threading.Event()

        def run_in_own_loop(label: str) -> None:
            async def body():
                lock = mgr.get_lock()
                barrier.wait()
                async with lock:
                    with events_guard:
                        events.append(f"{label}-acquired")
                        if len(events) == 2:
                            both_inside.set()
                    # Give the other thread a chance to also get in.
                    await asyncio.sleep(0.05)
                    with events_guard:
                        events.append(f"{label}-released")

            asyncio.run(body())

        t1 = threading.Thread(target=run_in_own_loop, args=("A",))
        t2 = threading.Thread(target=run_in_own_loop, args=("B",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Both threads got "-acquired" in BEFORE either "-released" —
        # proving no real mutual exclusion happened across the two loops.
        assert both_inside.is_set()
        acquired_count_before_any_release = 0
        for e in events:
            if e.endswith("released"):
                break
            acquired_count_before_any_release += 1
        assert acquired_count_before_any_release == 2
