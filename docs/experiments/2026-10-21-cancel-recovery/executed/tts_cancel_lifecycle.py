"""Private cancellation candidate. Never imported by the production bridge."""

import asyncio
import contextvars
import threading
import time
from functools import wraps

from starlette.responses import StreamingResponse

RAW_CANCEL = contextvars.ContextVar("raw_tts_cancel", default=None)


def cancellable_raw(factory):
    """Check before/after each raw chunk, before the handler discards silence.

    No sampling, chunk size, EOS, waveform or amplitude rules are changed.
    An in-flight prefill/decode cannot be interrupted within a CUDA operation.
    """

    @wraps(factory)
    def generate(*args, **kwargs):
        cancel = RAW_CANCEL.get()
        if cancel is not None and cancel.is_set():
            return
        inner = iter(factory(*args, **kwargs))
        try:
            while cancel is None or not cancel.is_set():
                try:
                    part = next(inner)
                except StopIteration:
                    return
                if cancel is not None and cancel.is_set():
                    return
                yield part
        finally:
            close = getattr(inner, "close", None)
            if close is not None:
                close()

    return generate


class StreamJob:
    """One owned worker; cancelling HTTP never releases a live inference lock."""

    def __init__(self, generate, lock, tasks, trace=None):
        self.generate, self.lock = generate, lock
        self.trace = trace or (lambda *_: None)
        self.cancel = threading.Event()
        self.chunks = asyncio.Queue(maxsize=8)
        self.loop = asyncio.get_running_loop()
        self.phase = "queued"
        self.error = None
        self.cleanup_error = None
        self.queue_peak = 0
        self.max_put_wait_ms = 0.0
        self.task = asyncio.create_task(self.run())
        tasks.add(self.task)

        def done(task):
            tasks.discard(task)
            if not task.cancelled():
                task.exception()  # Retrieve unexpected worker failures as well.
            self.trace("worker_exit")
            if self.phase == "queued":
                self.phase = "done"
                self._wake_cancelled_reader()

        self.task.add_done_callback(done)

    def _wake_cancelled_reader(self):
        # Called on the event loop only. Pending PCM is invalid after stop().
        while not self.chunks.empty():
            self.chunks.get_nowait()
        self.chunks.put_nowait(None)

    def stop(self):
        if not self.cancel.is_set():
            self.trace("cancel_signal")
        self.cancel.set()
        self._wake_cancelled_reader()
        if self.phase == "queued":
            self.trace("queue_skipped")
            self.task.cancel()

    async def _put(self, part):
        if self.cancel.is_set():
            return
        await self.chunks.put(part)
        self.queue_peak = max(self.queue_peak, self.chunks.qsize())

    def _send(self, part):
        """Backpressure the thread, with a cancellable single pending put."""
        if self.cancel.is_set():
            return False
        started = time.perf_counter()
        future = asyncio.run_coroutine_threadsafe(self._put(part), self.loop)
        try:
            while not self.cancel.is_set():
                try:
                    future.result(timeout=0.025)
                    return not self.cancel.is_set()
                except TimeoutError:
                    continue
            future.cancel()
            return False
        finally:
            self.max_put_wait_ms = max(
                self.max_put_wait_ms, (time.perf_counter() - started) * 1000
            )

    def produce(self):
        token = RAW_CANCEL.set(self.cancel)
        inner = None
        try:
            if not self.cancel.is_set():
                inner = iter(self.generate())
                for part in inner:
                    if not self._send(part):
                        break
        except Exception as exc:  # noqa: BLE001 - passed to the body consumer
            self.error = exc
        finally:
            try:
                if inner is not None and hasattr(inner, "close"):
                    inner.close()
            except Exception as exc:  # noqa: BLE001 - preserve the original error
                self.cleanup_error = exc
                if self.error is None:
                    self.error = exc
            finally:
                RAW_CANCEL.reset(token)
                self._send(None)

    async def run(self):
        try:
            async with self.lock:
                if self.cancel.is_set():
                    self.trace("queue_skipped")
                    return
                self.phase = "running"
                work = asyncio.create_task(asyncio.to_thread(self.produce))
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    self.stop()
                    # asyncio cancellation cannot interrupt the running thread.
                    # Even repeated cancels must join it before __aexit__ unlocks.
                    while not work.done():
                        try:
                            await asyncio.shield(work)
                        except asyncio.CancelledError:
                            continue
                    work.result()
                    raise
        finally:
            skipped = self.phase == "queued"
            self.phase = "done"
            if skipped:
                self._wake_cancelled_reader()

    async def body(self):
        try:
            while True:
                item = await self.chunks.get()
                if self.cancel.is_set():
                    return
                if item is None:
                    if self.error is not None:
                        raise self.error
                    return
                yield item
        finally:
            self.stop()


class JobResponse(StreamingResponse):
    """Observe disconnects even before first PCM, for ASGI 2.3 and 2.4."""

    def __init__(self, job):
        self.job = job
        super().__init__(
            job.body(),
            media_type="audio/L16;rate=16000;channels=1",
            headers={"X-Sample-Rate": "16000"},
        )

    async def __call__(self, scope, receive, send):
        stream = asyncio.create_task(self.stream_response(send))
        disconnect = asyncio.create_task(self.listen_for_disconnect(receive))
        try:
            completed, _ = await asyncio.wait(
                (stream, disconnect), return_when=asyncio.FIRST_COMPLETED
            )
            for task in completed:
                task.result()
        finally:
            self.job.stop()
            stream.cancel()
            disconnect.cancel()
            await asyncio.gather(stream, disconnect, return_exceptions=True)
            await self.body_iterator.aclose()
        if self.background is not None:
            await self.background()
