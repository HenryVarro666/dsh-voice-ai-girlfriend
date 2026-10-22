"""CPU checks for generation-end experiment measurements, not model quality."""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from generation_end_metrics import audio_stats, choose_candidate, classify_stop


class GenerationMetricsTests(unittest.TestCase):
    def test_token_cap_is_not_reported_as_natural_eos(self):
        self.assertEqual(classify_stop([7, 9], 9, 2, 2), "max_tokens")
        self.assertEqual(classify_stop([7, 9], 9, 1, 2), "eos")
        self.assertEqual(classify_stop([7, 8], 9, 1, 2), "other")

    def test_quiet_tail_measurement_does_not_mutate_audio(self):
        audio = np.array([0, 0, 0.1, -0.2, 0, 0, 0], dtype=np.float32)
        before = audio.copy()
        result = audio_stats(audio, 1000, 0.003)
        self.assertEqual(result["leading_ms"], 2)
        self.assertEqual(result["tail_ms"], 3)
        np.testing.assert_array_equal(audio, before)

    def test_all_quiet_is_explicit_instead_of_a_successful_empty_tail(self):
        result = audio_stats(np.zeros(100), 1000, 0.003)
        self.assertTrue(result["all_quiet"])
        self.assertEqual(result["duration_ms"], 100)

    def test_candidate_with_content_regression_is_rejected(self):
        baseline = {"eos": True, "edits": 0, "long_tails": 1, "tail_ms": 5000}
        candidates = {
            "temperature_05": {"eos": True, "edits": 1, "long_tails": 0, "tail_ms": 0}
        }
        self.assertIsNone(choose_candidate(baseline, candidates))

    def test_candidate_requires_eos_and_better_tail_measurement(self):
        baseline = {"eos": True, "edits": 0, "long_tails": 1, "tail_ms": 5000}
        candidates = {
            "temperature_05": {
                "eos": True,
                "edits": 0,
                "long_tails": 0,
                "tail_ms": 100,
            },
            "repetition_115": {
                "eos": False,
                "edits": 0,
                "long_tails": 0,
                "tail_ms": 50,
            },
        }
        self.assertEqual(choose_candidate(baseline, candidates), "temperature_05")


if __name__ == "__main__":
    unittest.main()
