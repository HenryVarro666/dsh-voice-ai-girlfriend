"""Private cancellation candidate. Never imported by the production bridge."""

import asyncio
import contextvars
import threading
from functools import wraps

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
    def __init__(self, generate, lock, tasks, trace=None):
        self.generate, self.lock, self.trace = (
            generate,
            lock,
            trace or (lambda *_: None),
        )
        self.cancel = threading.Event()
        self.chunks = asyncio.Queue()
        self.loop = asyncio.get_running_loop()
        self.phase = "queued"
        self.task = asyncio.create_task(self.run())
        tasks.add(self.task)

        def done(task):
            tasks.discard(task)
            self.trace("worker_exit")
            # Also wakes a consumer if cancellation won before run() started.
            if self.phase == "queued":
                self.phase = "done"
                self.chunks.put_nowait(None)

        self.task.add_done_callback(done)

    def stop(self):
        if not self.cancel.is_set():
            self.trace("cancel_signal")
        self.cancel.set()
        if self.phase == "queued":
            self.trace("queue_skipped")
            self.task.cancel()

    def produce(self):
        token = RAW_CANCEL.set(self.cancel)
        inner = None
        try:
            if self.cancel.is_set():
                return
            inner = iter(self.generate())
            for part in inner:
                if self.cancel.is_set():
                    return
                self.loop.call_soon_threadsafe(self.chunks.put_nowait, part)
        except Exception as exc:  # noqa: BLE001 - forwarded to the response consumer
            self.loop.call_soon_threadsafe(self.chunks.put_nowait, exc)
        finally:
            try:
                if inner is not None and hasattr(inner, "close"):
                    inner.close()
            finally:
                RAW_CANCEL.reset(token)
                self.loop.call_soon_threadsafe(self.chunks.put_nowait, None)

    async def run(self):
        try:
            async with self.lock:
                if self.cancel.is_set():
                    self.trace("queue_skipped")
                    return
                self.phase = "running"
                await asyncio.to_thread(self.produce)
        finally:
            skipped = self.phase == "queued"
            self.phase = "done"
            if skipped:
                self.chunks.put_nowait(None)

    async def body(self):
        try:
            while (item := await self.chunks.get()) is not None:
                if self.cancel.is_set():
                    return
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            self.stop()
