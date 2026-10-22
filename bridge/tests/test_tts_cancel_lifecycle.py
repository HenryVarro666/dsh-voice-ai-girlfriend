"""Real lifecycle boundaries with blocking fake GPU work, without torch."""

import asyncio
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from tts_cancel_lifecycle import RAW_CANCEL, JobResponse, StreamJob, cancellable_raw


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_before_worker_is_scheduled_wakes_consumer(self):
        calls, tasks = [], set()
        job = StreamJob(lambda: calls.append(1) or iter([]), asyncio.Lock(), tasks)
        job.stop()
        await asyncio.gather(job.task, return_exceptions=True)
        self.assertEqual([x async for x in job.body()], [])
        self.assertFalse(calls)
        self.assertFalse(tasks)

    async def test_cancelled_waiter_never_calls_model_and_leaves_task_set(self):
        lock, tasks, calls = asyncio.Lock(), set(), []
        await lock.acquire()
        job = StreamJob(lambda: calls.append(1) or iter([b"pcm"]), lock, tasks)
        await asyncio.sleep(0)
        job.stop()
        await asyncio.wait_for(asyncio.gather(job.task, return_exceptions=True), 0.2)
        await asyncio.sleep(0)
        self.assertFalse(calls)
        self.assertFalse(tasks)
        self.assertTrue(lock.locked())
        lock.release()

    async def test_running_thread_keeps_lock_until_it_has_exited(self):
        entered, release = threading.Event(), threading.Event()
        lock, tasks = asyncio.Lock(), set()

        def generate():
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test release missing")
            yield b"should not be sent"

        job = StreamJob(generate, lock, tasks)
        await asyncio.to_thread(entered.wait, 2)
        job.stop()
        await asyncio.sleep(0)
        self.assertTrue(lock.locked())
        self.assertFalse(job.task.done())
        release.set()
        await job.task
        self.assertFalse(lock.locked())
        self.assertEqual([x async for x in job.body()], [])

    async def test_disconnect_before_first_pcm_stops_silent_generator(self):
        cancel_seen, closed = threading.Event(), threading.Event()
        count = []

        def raw():
            try:
                for n in range(100):
                    count.append(n)
                    if n == 1:
                        cancel_seen.set()
                        if not release.wait(2):
                            raise RuntimeError("test release missing")
                    yield b"silence"
            finally:
                closed.set()

        def handler():
            for _ in cancellable_raw(raw)():
                pass  # Like the deployed handler, withholds all leading silence.
            return
            yield b"never"

        release = threading.Event()
        job = StreamJob(handler, asyncio.Lock(), set())
        await asyncio.to_thread(cancel_seen.wait, 2)
        reading = asyncio.create_task(anext(job.body()))
        await asyncio.sleep(0)
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)
        release.set()
        await job.task
        self.assertTrue(closed.is_set())
        self.assertEqual(count, [0, 1])

    async def test_natural_output_is_identical_and_context_is_reset(self):
        values = [b"\0\0", b"quiet ending", b"tail\0"]
        factory = cancellable_raw(lambda: iter(values))
        job = StreamJob(factory, asyncio.Lock(), set())
        self.assertEqual([x async for x in job.body()], values)
        await job.task
        self.assertIsNone(RAW_CANCEL.get())

    async def test_failure_reaches_consumer_and_releases_lock(self):
        lock = asyncio.Lock()

        def broken():
            yield b"first"
            raise ValueError("model failure")

        job = StreamJob(broken, lock, set())
        with self.assertRaisesRegex(ValueError, "model failure"):
            async for _ in job.body():
                pass
        await job.task
        self.assertFalse(lock.locked())


class LifecycleStressTests(unittest.IsolatedAsyncioTestCase):
    async def test_backpressured_pcm_is_complete_and_pending_put_cancels(self):
        values = [bytes([n]) * 1024 for n in range(32)]
        job = StreamJob(lambda: iter(values), asyncio.Lock(), set())
        output = []
        async for chunk in job.body():
            output.append(chunk)
            await asyncio.sleep(0.001)
        await job.task
        self.assertEqual(output, values)
        self.assertEqual(job.queue_peak, 8)

        blocked = StreamJob(lambda: iter(values), asyncio.Lock(), set())
        await asyncio.sleep(0.03)
        self.assertEqual(blocked.chunks.qsize(), 8)
        blocked.stop()
        await asyncio.wait_for(blocked.task, 0.2)
        await asyncio.sleep(0)
        self.assertFalse(
            any(
                t.get_coro().__qualname__ == "StreamJob._put"
                for t in asyncio.all_tasks()
                if t is not asyncio.current_task()
            )
        )

    async def test_repeated_worker_cancellation_still_joins_thread(self):
        entered, release = threading.Event(), threading.Event()
        lock = asyncio.Lock()

        def generate():
            entered.set()
            release.wait(2)
            yield b"pcm"

        job = StreamJob(generate, lock, set())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            for _ in range(3):
                job.task.cancel()
                await asyncio.sleep(0.005)
                self.assertTrue(lock.locked())
        finally:
            release.set()
            await asyncio.gather(job.task, return_exceptions=True)
        self.assertFalse(lock.locked())

    async def test_send_failure_closes_body_and_allows_next_request(self):
        lock = asyncio.Lock()
        job = StreamJob(lambda: iter([b"a"] * 100), lock, set())

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            if message.get("body"):
                raise OSError("broken socket")

        with self.assertRaisesRegex(OSError, "broken socket"):
            await JobResponse(job)({"type": "http"}, receive, send)
        await asyncio.wait_for(job.task, 0.2)
        self.assertTrue(job.cancel.is_set())
        following = StreamJob(lambda: iter([b"next"]), lock, set())
        self.assertEqual([x async for x in following.body()], [b"next"])
        await following.task

    async def test_disconnect_before_audio_for_both_asgi_versions(self):
        for version in ("2.3", "2.4"):
            with self.subTest(asgi=version):
                entered, release = threading.Event(), threading.Event()
                lock, disconnected = asyncio.Lock(), asyncio.Event()
                sent = []

                def generate(entered=entered, release=release):
                    entered.set()
                    release.wait(2)
                    yield b"pcm"

                async def receive(disconnected=disconnected):
                    await disconnected.wait()
                    return {"type": "http.disconnect"}

                async def send(message, sent=sent):
                    sent.append(message)

                job = StreamJob(generate, lock, set())
                response = JobResponse(job)
                serving = asyncio.create_task(
                    response(
                        {"type": "http", "asgi": {"spec_version": version}},
                        receive,
                        send,
                    )
                )
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    disconnected.set()
                    await asyncio.wait_for(serving, 0.15)
                    self.assertTrue(job.cancel.is_set())
                    self.assertTrue(lock.locked())
                    self.assertFalse(any(m.get("body") for m in sent))
                finally:
                    release.set()
                    job.stop()
                    await asyncio.gather(serving, job.task, return_exceptions=True)

    async def test_slow_consumer_bounds_pending_pcm(self):
        paused = threading.Event()

        def generate():
            for _ in range(1000):
                yield b"x" * 1024
            paused.set()

        job = StreamJob(generate, asyncio.Lock(), set())
        try:
            await asyncio.sleep(0.08)
            self.assertLessEqual(job.chunks.qsize(), 8)
            self.assertFalse(paused.is_set(), "Producer must wait for a slow reader")
        finally:
            job.stop()
            await asyncio.wait_for(asyncio.gather(job.task, return_exceptions=True), 2)

    async def test_external_worker_cancel_keeps_gpu_lock_until_thread_exit(self):
        entered, release = threading.Event(), threading.Event()
        lock = asyncio.Lock()

        def generate():
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test release missing")
            yield b"pcm"

        job = StreamJob(generate, lock, set())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            job.task.cancel()
            await asyncio.sleep(0.02)
            self.assertTrue(lock.locked(), "Cancellation cannot unlock active GPU work")
            self.assertFalse(job.task.done())
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(job.task, return_exceptions=True), 2)

    async def test_close_error_is_observed_and_context_still_resets(self):
        class BrokenClose:
            def __iter__(self):
                return self

            def __next__(self):
                raise StopIteration

            def close(self):
                raise ValueError("close failed")

        job = StreamJob(BrokenClose, asyncio.Lock(), set())
        with self.assertRaisesRegex(ValueError, "close failed"):
            async for _ in job.body():
                pass
        await asyncio.gather(job.task, return_exceptions=True)
        self.assertIsNone(RAW_CANCEL.get())


if __name__ == "__main__":
    unittest.main()
