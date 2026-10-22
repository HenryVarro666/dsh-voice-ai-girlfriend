"""Guard against counting repeated ASR calls as independent TTS successes."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from recheck_metrics import repeated_summary
from report_numeric_recheck import paired_outcomes


class RecheckMetricsTests(unittest.TestCase):
    def setUp(self):
        self.suite = [
            {"id": "x", "repeats": 3, "sha256": "wav", "case": {"code": "110022"}}
        ]

    def row(self, repeat, status="match", error=False):
        base = {"id": "x", "repeat": repeat, "wav_sha256": "wav"}
        return base | (
            {"error": "timeout"}
            if error
            else {
                "exact": True,
                "field_status": status,
                "observed_code": "110022" if status == "match" else "10022",
                "normalized_asr_sha256": status,
            }
        )

    def test_majority_match_does_not_become_stable_success(self):
        result = repeated_summary(
            self.suite, [self.row(0), self.row(1), self.row(2, "mismatch")]
        )["x"]
        self.assertFalse(result["stable_fields"])
        self.assertTrue(result["any_fields_match"])
        self.assertTrue(result["field_result_changes"])

    def test_missing_and_failed_calls_cannot_pass(self):
        for rows in (
            [self.row(0), self.row(1)],
            [self.row(0), self.row(1), self.row(2, error=True)],
        ):
            result = repeated_summary(self.suite, rows)["x"]
            self.assertFalse(result["stable_fields"])
            self.assertFalse(result["stable_exact"])
        self.assertEqual(repeated_summary(self.suite, [self.row(0)])["x"]["missing"], 2)

    def test_all_agreeing_calls_are_one_stable_audio(self):
        result = repeated_summary(self.suite, [self.row(i) for i in range(3)])
        self.assertEqual(len(result), 1)
        self.assertTrue(result["x"]["stable_fields"])

    def test_duplicate_and_changed_wav_are_rejected(self):
        with self.assertRaises(ValueError):
            repeated_summary(self.suite, [self.row(0), self.row(0)])
        bad = self.row(0) | {"wav_sha256": "other"}
        with self.assertRaises(ValueError):
            repeated_summary(self.suite, [bad])

    def test_pairs_keep_diagnostic_and_new_input_separate(self):
        suite, checks = [], {}
        for phase in ("diagnostic", "holdout"):
            for profile in ("baseline", "xvector"):
                key = f"{phase}-{profile}"
                suite.append(
                    {
                        "id": key,
                        "phase": phase,
                        "text_index": 0,
                        "seed": 1,
                        "profile": profile,
                    }
                )
                checks[key] = {
                    "stable_fields": profile == "baseline" or phase == "diagnostic"
                }
        result = paired_outcomes(suite, checks, "stable_fields")
        self.assertEqual(result["pairs"], 2)
        self.assertEqual(
            result["outcomes"], {"both_stable": 1, "baseline_only_stable": 1}
        )

    def test_incomplete_pair_cannot_be_reported_as_a_candidate_win(self):
        with self.assertRaises(ValueError):
            paired_outcomes(
                [{"id": "x", "pair_id": "p01", "profile": "xvector"}],
                {"x": {"stable_exact": True}},
                "stable_exact",
            )


if __name__ == "__main__":
    unittest.main()
