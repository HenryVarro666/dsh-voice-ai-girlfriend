"""Sample preservation and streaming lifecycle for the unshipped tail prototype."""

import asyncio
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stream_tail_safe import ExactZeroTailGuard


def pcm(value, samples):
    return np.full(samples, value, dtype="<i2").tobytes()


def strict_guard():
    return ExactZeroTailGuard()


def process(data, block=1024):
    guard = strict_guard()
    output = b"".join(
        guard.process(data[i : i + block] for i in range(0, len(data), block))
    )
    return output, guard


class ExactZeroTests(unittest.TestCase):
    def test_subthreshold_weak_word_is_preserved_in_full(self):
        for value in (1, -1, 70, -70):
            audio = pcm(1000, 500) + pcm(value, 20000)
            self.assertEqual(process(audio)[0], audio)

    def test_weak_word_before_zero_tail_is_preserved(self):
        prefix = pcm(1000, 100) + pcm(1, 24000)
        self.assertEqual(process(prefix + pcm(0, 32000))[0], prefix + pcm(0, 4800))

    def test_noise_is_not_deleted_as_if_it_were_digital_silence(self):
        noise = np.random.default_rng(20261020).integers(-20, 21, 40000, dtype=np.int16)
        audio = pcm(1000, 100) + noise.astype("<i2").tobytes()
        self.assertEqual(process(audio)[0], audio)

    def test_internal_pause_and_extreme_samples_are_unchanged(self):
        audio = pcm(-32768, 100) + pcm(0, 64000) + pcm(1, 600)
        self.assertEqual(process(audio)[0], audio)

    def test_exact_zero_tail_can_be_shortened_at_any_chunk_boundary(self):
        prefix = pcm(1, 10)
        for block in (2, 62, 1024, 65536):
            self.assertEqual(
                process(prefix + pcm(0, 16001), block)[0], prefix + pcm(0, 4800)
            )

    def test_all_zeros_and_overflow_fail_open(self):
        for audio in (pcm(0, 170000), pcm(1, 100) + pcm(0, 170000)):
            output, guard = process(audio)
            self.assertEqual(output, audio)
            self.assertLessEqual(guard.peak_pending_bytes, 256000)


class AsyncSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_consumer_close_stops_pulling_source(self):
        from stream_tail_safe import AsyncTailStream

        closed = False
        pulls = 0

        async def source():
            nonlocal closed, pulls
            try:
                pulls += 1
                yield pcm(1000, 1600)
                pulls += 1
                yield pcm(1000, 1600)
            finally:
                closed = True

        adapter = AsyncTailStream()
        output = adapter.process(source())
        self.assertEqual(await anext(output), pcm(1000, 1600))
        await output.aclose()
        self.assertTrue(closed)
        self.assertEqual(pulls, 1)
        self.assertEqual(adapter.guard.pending_bytes, 0)

    async def test_slow_internal_pause_is_released_before_voice_resumes(self):
        from stream_tail_safe import AsyncTailStream

        resumed = False

        async def source():
            nonlocal resumed
            yield pcm(1000, 1600) + pcm(0, 9600)
            await asyncio.sleep(0.2)
            resumed = True
            yield pcm(70, 1600)

        adapter = AsyncTailStream(max_hold_s=0.03)
        parts, released_early = [], False
        async for data in adapter.process(source()):
            parts.append(data)
            if len(b"".join(parts)) == (1600 + 9600) * 2:
                released_early = not resumed
        self.assertTrue(released_early)
        self.assertEqual(
            b"".join(parts), pcm(1000, 1600) + pcm(0, 9600) + pcm(70, 1600)
        )

    async def test_cancel_clears_pending_and_closes_blocked_source(self):
        from stream_tail_safe import AsyncTailStream

        closed = asyncio.Event()
        pending = asyncio.Event()

        async def source():
            try:
                yield pcm(1000, 16000) + pcm(0, 10000)
                pending.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

        adapter = AsyncTailStream()
        parts = []

        async def consume():
            async for data in adapter.process(source()):
                parts.append(data)

        task = asyncio.create_task(consume())
        await asyncio.wait_for(pending.wait(), 1)
        before = b"".join(parts)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertTrue(closed.is_set())
        self.assertEqual(adapter.guard.pending_bytes, 0)
        self.assertEqual(b"".join(parts), before)

    async def test_network_byte_boundaries_and_zero_tail(self):
        from stream_tail_safe import AsyncTailStream

        audio = pcm(1, 30) + pcm(0, 32000)

        async def source():
            for i in range(0, len(audio), 63):
                yield audio[i : i + 63]

        adapter = AsyncTailStream()
        output = b"".join([data async for data in adapter.process(source())])
        self.assertEqual(output, pcm(1, 30) + pcm(0, 4800))

    async def test_source_error_discards_unsent_tail(self):
        from stream_tail_safe import AsyncTailStream

        async def source():
            yield pcm(1000, 16000) + pcm(0, 20000)
            raise RuntimeError("owned source failed")

        adapter = AsyncTailStream()
        parts = []
        with self.assertRaisesRegex(RuntimeError, "owned source failed"):
            async for data in adapter.process(source()):
                parts.append(data)
        self.assertEqual(b"".join(parts), pcm(1000, 16000) + pcm(0, 4800))
        self.assertEqual(adapter.guard.pending_bytes, 0)

    async def test_incomplete_pcm_sample_is_reported(self):
        from stream_tail_safe import AsyncTailStream

        async def source():
            yield b"x"

        with self.assertRaisesRegex(ValueError, "incomplete"):
            async for _ in AsyncTailStream().process(source()):
                pass


if __name__ == "__main__":
    unittest.main()
