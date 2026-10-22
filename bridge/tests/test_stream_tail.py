"""Contracts and known counterexample for the experimental PCM tail guard."""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stream_tail import TailGuard


def pcm(value, count):
    return np.full(count, value, dtype="<i2").tobytes()


def apply(data, block=1024):
    guard = TailGuard()
    output = b"".join(
        guard.process(data[i : i + block] for i in range(0, len(data), block))
    )
    return output, guard


class TailTests(unittest.TestCase):
    def test_short_tail_and_threshold_boundary_are_unchanged(self):
        for count in (0, 4800, 16000):
            data = pcm(99, 200) + pcm(98, count)
            self.assertEqual(apply(data)[0], data)

    def test_long_tail_keeps_prefix_and_300ms(self):
        data = pcm(-32768, 500) + pcm(10, 16001)
        result, guard = apply(data)
        self.assertEqual(result, data[: (500 + 4800) * 2])
        self.assertEqual(guard.removed_samples, 16001 - 4800)

    def test_long_internal_pause_is_byte_identical(self):
        data = pcm(2000, 1000) + pcm(27, 64000) + pcm(3000, 2000)
        self.assertEqual(apply(data)[0], data)

    def test_all_quiet_has_no_basis_for_cropping(self):
        data = pcm(1, 170000)
        self.assertEqual(apply(data)[0], data)

    def test_overflow_fails_open_and_stays_bounded(self):
        data = pcm(1000, 1000) + pcm(20, 160000)
        result, guard = apply(data)
        self.assertEqual(result, data)
        self.assertEqual(guard.overflows, 1)
        self.assertLessEqual(guard.peak_pending_bytes, 256000)

    def test_overflow_does_not_disable_later_tail(self):
        prefix = pcm(1000, 1000) + pcm(20, 160000) + pcm(1000, 500)
        self.assertEqual(apply(prefix + pcm(0, 20000))[0], prefix + pcm(0, 4800))

    def test_chunk_boundaries_do_not_change_samples(self):
        data = pcm(1000, 79) + pcm(20, 27000) + pcm(1000, 53) + pcm(20, 33000)
        expected = data[: (79 + 27000 + 53 + 4800) * 2]
        for size in (2, 62, 1024, 8186, len(data)):
            self.assertEqual(apply(data, size)[0], expected)

    def test_empty_input(self):
        self.assertEqual(apply(b"")[0], b"")

    def test_odd_byte_input_is_rejected(self):
        with self.assertRaises(ValueError):
            apply(b"x")

    def test_cancel_discards_pending_and_closes_source(self):
        state = {"cancelled": False, "closed": False}

        def source():
            try:
                yield pcm(1000, 100)
                yield pcm(0, 20000)
                state["cancelled"] = True
            finally:
                state["closed"] = True

        guard = TailGuard()
        result = b"".join(guard.process(source(), lambda: state["cancelled"]))
        self.assertEqual(result, pcm(1000, 100) + pcm(0, 4800))
        self.assertEqual(guard.pending_bytes, 0)
        self.assertTrue(state["closed"])

    def test_already_cancelled_does_not_pull_another_chunk(self):
        def source():
            self.fail("cancelled stream must not pull synthesis")
            yield b""

        self.assertEqual(list(TailGuard().process(source(), lambda: True)), [])

    def test_error_does_not_flush_pending(self):
        def source():
            yield pcm(1000, 100)
            yield pcm(0, 20000)
            raise RuntimeError("synthesis failed")

        guard = TailGuard()
        output = guard.process(source())
        self.assertEqual(next(output), pcm(1000, 100))
        self.assertEqual(next(output), pcm(0, 4800))
        with self.assertRaisesRegex(RuntimeError, "synthesis failed"):
            list(output)
        self.assertEqual(guard.pending_bytes, 0)

    def test_subthreshold_tail_is_a_known_false_crop(self):
        # A synthetic soft word below the amplitude threshold gets shortened.
        # Passing this test documents the limitation; it does not prove safety.
        weak_word = pcm(70, 16001)
        data = pcm(1000, 100) + weak_word
        self.assertLess(len(apply(data)[0]), len(data))

    def test_retained_prefix_is_released_before_eof(self):
        state = {"ended": False}

        def source():
            yield pcm(1000, 100) + pcm(0, 5000)
            state["ended"] = True

        output = TailGuard().process(source())
        self.assertEqual(next(output), pcm(1000, 100))
        self.assertEqual(next(output), pcm(0, 4800))
        self.assertFalse(state["ended"])
        self.assertEqual(b"".join(output), pcm(0, 200))

    def test_conservative_threshold_keeps_weak_tail(self):
        data = pcm(1000, 100) + pcm(70, 16001) + pcm(0, 20000)
        result = b"".join(TailGuard(threshold=3).process([data]))
        self.assertEqual(result, data[: (100 + 16001 + 4800) * 2])

    def test_even_conservative_threshold_has_a_quieter_counterexample(self):
        data = pcm(1000, 100) + pcm(2, 16001)
        self.assertLess(
            len(b"".join(TailGuard(threshold=3).process([data]))), len(data)
        )

    def test_async_adapter_contract_matches_iterator(self):
        chunks = [pcm(1000, 100), pcm(2, 17000)]
        guard = TailGuard(threshold=3)
        output = [part for chunk in chunks for part in guard.feed(chunk)]
        output.extend(guard.finish())
        guard.close()
        self.assertEqual(
            b"".join(output), b"".join(TailGuard(threshold=3).process(chunks))
        )

    def test_invalid_threshold_is_rejected(self):
        for value in (0, 32769):
            with self.assertRaises(ValueError):
                TailGuard(value)


if __name__ == "__main__":
    unittest.main()
