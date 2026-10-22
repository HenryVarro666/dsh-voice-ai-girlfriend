#!/usr/bin/env python3
"""Summarize every repetition-validation output, retaining all failures."""

import argparse
import json
from pathlib import Path

from report_generation_end import summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    args = parser.parse_args()
    rows = [
        json.loads(s) for s in (args.folder / "trials.jsonl").read_text().splitlines()
    ]
    result = {}
    for category in ["all"] + sorted({r["category"] for r in rows}):
        selected = [r for r in rows if category == "all" or r["category"] == category]
        result[category] = {
            p: summarize([r for r in selected if r["profile"] == p])
            for p in ("baseline", "repetition_115")
        }
        pair = {}
        for r in selected:
            pair.setdefault((r["text_index"], r["seed"]), {})[r["profile"]] = r
        comparison = {
            "candidate_edits_better": 0,
            "candidate_edits_worse": 0,
            "equal": 0,
            "incomplete_or_failed": 0,
        }
        for values in pair.values():
            if len(values) != 2 or any("error" in r for r in values.values()):
                comparison["incomplete_or_failed"] += 1
                continue
            delta = (
                values["repetition_115"]["edit_distance"]
                - values["baseline"]["edit_distance"]
            )
            comparison[
                "candidate_edits_better"
                if delta < 0
                else "candidate_edits_worse"
                if delta > 0
                else "equal"
            ] += 1
        result[category]["paired"] = comparison
    (args.folder / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
