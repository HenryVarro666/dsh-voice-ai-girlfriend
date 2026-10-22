"""Experimental end-only PCM16 tail guard; not enabled in the bridge.

16 kHz mono little-endian input only. Amplitude is not a speech detector:
quiet words can be mistaken for tails. Keep this out of production until
listening and streaming playback tests justify a safer policy.
"""

from collections.abc import Callable, Iterable, Iterator

import numpy as np


class TailGuard:
    """Hold a quiet suffix until voice resumes or normal EOF confirms a tail.

    Pending audio is capped at 8 seconds (256000 bytes). Overflow releases
    the original bytes and bypasses that quiet run. Source chunks and emitted
    copies use additional memory; the cap covers retained pending PCM only.
    """

    def __init__(self, threshold: int = 99):
        if not 1 <= threshold <= 32768:
            raise ValueError("threshold must be in PCM16 amplitude units")
        self.threshold = threshold
        self._pending = bytearray()
        self._voiced = False
        self._bypass = False
        self._quiet_samples = 0
        self.peak_pending_bytes = 0
        self.overflows = 0
        self.removed_samples = 0

    @property
    def pending_bytes(self):
        return len(self._pending)

    def _release(self):
        if self._pending:
            data = bytes(self._pending)
            self._pending.clear()
            yield data

    def feed(self, data: bytes) -> Iterator[bytes]:
        """Process one aligned PCM chunk, for sync or async stream adapters."""
        if len(data) % 2:
            raise ValueError("PCM16 chunks must contain whole samples")
        samples = np.frombuffer(data, dtype="<i2").astype(np.int32)
        active = np.flatnonzero(np.abs(samples) >= self.threshold)
        if len(active):
            self._voiced = True
            self._bypass = False
            self._quiet_samples = 0
            yield from self._release()
            end = (int(active[-1]) + 1) * 2
            yield data[:end]
            data = data[end:]
        if not data:
            return
        # The retained 300 ms can be sent immediately. Waiting for EOF even
        # for this safe prefix unnecessarily delays ordinary short pauses.
        early_samples = min(len(data) // 2, max(0, 4800 - self._quiet_samples))
        self._quiet_samples += len(data) // 2
        if early_samples:
            yield data[: early_samples * 2]
            data = data[early_samples * 2 :]
        if not data:
            return
        if self._bypass:
            yield data
        elif len(self._pending) + len(data) > 256000:
            self.overflows += 1
            self._bypass = True
            yield from self._release()
            yield data
        else:
            self._pending.extend(data)
            self.peak_pending_bytes = max(self.peak_pending_bytes, len(self._pending))

    def finish(self) -> Iterator[bytes]:
        """Flush only after a successful EOF; never call this on cancellation."""
        if self._voiced and self._quiet_samples > 16000:
            self.removed_samples = len(self._pending) // 2
            self._pending.clear()
        yield from self._release()

    def close(self):
        """Discard unsent audio on cancellation, errors or normal teardown."""
        self._pending.clear()

    def process(
        self, chunks: Iterable[bytes], cancelled: Callable[[], bool] = lambda: False
    ) -> Iterator[bytes]:
        """Consume one owned source; close it and drop pending PCM on exit."""
        source = iter(chunks)
        try:
            while not cancelled():
                try:
                    data = next(source)
                except StopIteration:
                    break
                if cancelled():
                    return
                for output in self.feed(data):
                    if cancelled():
                        return
                    yield output
            if cancelled():
                return
            yield from self.finish()
        finally:
            self.close()
            close = getattr(source, "close", None)
            if close is not None:
                close()
