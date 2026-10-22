"""Unshipped exact-zero tail policy. No model loading or production integration."""

import asyncio
from contextlib import suppress

from stream_tail import TailGuard


class ExactZeroTailGuard(TailGuard):
    """Preserve every nonzero sample; only numerical zero can form a tail."""

    def __init__(self):
        super().__init__(threshold=1)

    def fail_open(self):
        """Release this quiet run without cropping when waiting becomes unsafe."""
        self._bypass = True
        yield from self._release()


class AsyncTailStream:
    """Own one async PCM source and avoid retaining a pause past playable lead.

    The lead is an ideal PCM clock, not feedback from a browser device. Source
    stalls release pending bytes before EOF; late callbacks can still jitter.
    A single next-item task is kept alive across timeouts, never duplicated.
    """

    def __init__(self, max_hold_s=0.1):
        if max_hold_s <= 0:
            raise ValueError("max_hold_s must be positive")
        self.guard = ExactZeroTailGuard()
        self.max_hold_s = max_hold_s
        self.timed_releases = 0

    async def process(self, chunks):
        source = aiter(chunks)
        task = None
        carry = b""
        loop = asyncio.get_running_loop()
        cursor = loop.time()
        pending_since = None
        try:
            while True:
                if task is None:
                    task = asyncio.create_task(anext(source))
                timeout = None
                if self.guard.pending_bytes:
                    now = loop.time()
                    if pending_since is None:
                        pending_since = now
                    timeout = max(
                        0, min(pending_since + self.max_hold_s, cursor - 0.02) - now
                    )
                done, _ = await asyncio.wait({task}, timeout=timeout)
                if not done:
                    self.timed_releases += 1
                    for data in self.guard.fail_open():
                        cursor = max(cursor, loop.time()) + len(data) / 32000
                        yield data
                    pending_since = None
                    continue
                try:
                    packet = task.result()
                except StopAsyncIteration:
                    if carry:
                        raise ValueError("incomplete PCM16 sample at EOF") from None
                    for data in self.guard.finish():
                        yield data
                    return
                finally:
                    task = None
                packet = carry + packet
                end = len(packet) - len(packet) % 2
                carry = packet[end:]
                for data in self.guard.feed(packet[:end]):
                    cursor = max(cursor, loop.time()) + len(data) / 32000
                    yield data
                if not self.guard.pending_bytes:
                    pending_since = None
        finally:
            self.guard.close()
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await task
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()
