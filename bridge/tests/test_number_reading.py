import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from number_reading import check, integer, render


class NumberReadingTests(unittest.TestCase):
    def test_leading_zero_and_repeated_digits_survive(self):
        case = {"code": "001100"}
        self.assertEqual(render(case, True), "编号是零零一一零零。")
        self.assertEqual(check(case, "编号是零零一一零零")["status"], "match")
        self.assertEqual(check(case, "编号是001100")["status"], "match")

    def test_missing_and_transposed_digits_fail(self):
        case = {"code": "110022"}
        self.assertEqual(check(case, "编号是一零零二二")["status"], "mismatch")
        self.assertEqual(check(case, "编号是一零一零二二")["status"], "mismatch")

    def test_cardinal_code_and_ambiguous_tokens_are_not_guessed(self):
        self.assertEqual(
            check({"code": "110022"}, "编号是一千一百零二二")["status"], "unparsed"
        )
        self.assertIsNone(integer("一百零二二"))
        self.assertEqual(
            check({"code": "12"}, "编号一二编号一二")["status"], "unparsed"
        )

    def test_dates_and_minutes_use_values(self):
        case = {"code": "001100", "date": [2027, 1, 2], "time": [9, 5]}
        self.assertEqual(
            render(case, True),
            "编号是零零一一零零。日期是二零二七年一月二日，时间是九点五分。",
        )
        self.assertEqual(check(case, render(case, True))["status"], "match")
        self.assertEqual(
            check(case, "编号001100日期2027年1月2日时间9点50分")["status"], "mismatch"
        )

    def test_unrecognized_syllable_is_not_silently_truncated(self):
        for heard in ("编号是一零狼二零零三零", "编号是一一零ln二二"):
            self.assertEqual(check({"code": "110022"}, heard)["status"], "unparsed")

    def test_missing_date_is_not_success(self):
        case = {"code": "12", "date": [2026, 11, 11], "time": [15, 30]}
        self.assertEqual(check(case, "编号一二")["status"], "unparsed")
        self.assertEqual(integer("二十三"), 23)
        self.assertEqual(integer("十五"), 15)
