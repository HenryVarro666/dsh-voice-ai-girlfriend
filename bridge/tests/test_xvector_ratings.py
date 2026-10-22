"""Lock down A/B reversal, integrity-vs-preference and missing-rating handling."""

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from analyze_xvector_ratings import analyze


class ListeningRatingsTests(unittest.TestCase):
    def setUp(self):
        pairs = [
            {
                "id": f"p{i}",
                "source": "endings",
                "priority": True,
                "text": "测试句。",
                "seed": i,
                "mapping": {"A": {"mode": a}, "B": {"mode": b}},
            }
            for i, (a, b) in enumerate(
                (("baseline", "xvector"), ("xvector", "baseline"))
            )
        ]
        hashed = hashlib.sha256(
            json.dumps(pairs, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        self.mapping = {"pack_id": "test", "mapping_sha256": hashed, "pairs": pairs}
        self.export = {
            "version": 1,
            "pack_id": "test",
            "mapping_sha256": hashed,
            "revealed": False,
            "ratings": [],
        }

    def test_same_letter_decodes_to_different_modes(self):
        self.export["ratings"] = [
            {"id": "p0", "timbre": "A"},
            {"id": "p1", "timbre": "A"},
        ]
        outcomes = analyze(self.export, self.mapping)["groups"]["all"]["dimensions"][
            "timbre"
        ]
        self.assertEqual(outcomes, {"baseline_preferred": 1, "xvector_preferred": 1})

    def test_bad_ending_is_separate_from_preference_with_both_complete(self):
        self.export["ratings"] = [
            {"id": "p0", "ending": "B_bad"},
            {"id": "p1", "ending": "A"},
        ]
        outcomes = analyze(self.export, self.mapping)["groups"]["all"]["dimensions"][
            "ending"
        ]
        self.assertEqual(
            outcomes, {"xvector_ending_problem": 1, "xvector_preferred": 1}
        )

    def test_no_votes_are_not_a_no_regression_verdict(self):
        result = analyze(self.export, self.mapping)
        self.assertEqual(result["evidence_status"], "no_human_choices")
        self.assertEqual(len(result["absent_rows"]), 2)
        self.assertEqual(
            result["groups"]["all"]["dimensions"]["ending"], {"pending": 2}
        )

    def test_wrong_mapping_is_rejected(self):
        bad = copy.deepcopy(self.export)
        bad["mapping_sha256"] = "wrong"
        with self.assertRaises(ValueError):
            analyze(bad, self.mapping)

    def test_duplicate_and_unknown_values_are_rejected(self):
        for ratings in ([{"id": "p0"}, {"id": "p0"}], [{"id": "p0", "ending": "good"}]):
            self.export["ratings"] = ratings
            with self.assertRaises(ValueError):
                analyze(self.export, self.mapping)

    def test_uncertain_both_bad_and_partial_votes_keep_their_denominators(self):
        self.export["ratings"] = [
            {"id": "p0", "timbre": "unsure", "ending": "both_bad"}
        ]
        result = analyze(self.export, self.mapping)["groups"]["all"]
        self.assertEqual(result["completed_pairs"], 0)
        self.assertEqual(result["pairs_with_any_choice"], 1)
        self.assertEqual(result["dimensions"]["timbre"], {"unsure": 1, "pending": 1})
        self.assertEqual(result["dimensions"]["ending"], {"both_bad": 1, "pending": 1})

    def test_revealed_export_and_qa_contamination_are_explicit(self):
        self.export["revealed"] = True
        self.assertTrue(analyze(self.export, self.mapping)["revealed_at_export"])
        self.export["ratings"] = [{"id": "p0", "notes": "QA技术验证，非人工评分"}]
        with self.assertRaises(ValueError):
            analyze(self.export, self.mapping)


if __name__ == "__main__":
    unittest.main()
