#!/usr/bin/env python3
"""Recompute numerical summaries without a GPU or reference audio."""

import argparse
import json
import statistics
from pathlib import Path


def median(rows, field):
    values = [r[field] for r in rows if r.get(field) is not None]
    return statistics.median(values) if values else None


def summarize(rows):
    valid = [r for r in rows if "error" not in r]
    tails = [r["pcm"]["tail_ms"] for r in valid if r["pcm"]["tail_ms"] is not None]
    return {
        "trials": len(rows),
        "errors": sum("error" in r for r in rows),
        "eos": sum(r.get("stop_reason") == "eos" for r in rows),
        "max_tokens": sum(r.get("stop_reason") == "max_tokens" for r in rows),
        "asr_exact": sum(r.get("asr_exact", False) for r in rows),
        "asr_ending_match": sum(r.get("asr_ending_match", False) for r in rows),
        "edit_distance": sum(r.get("edit_distance", 0) for r in rows),
        "long_tails": sum(t > 1000 for t in tails),
        "tail_total_ms": sum(tails),
        "tail_max_ms": max(tails, default=None),
        "first_voiced_median_ms": median(valid, "first_voiced_ms"),
        "generation_median_ms": median(valid, "generation_ms"),
        "frames_median": median(valid, "frames"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True, type=Path)
    args = parser.parse_args()
    rows = [
        json.loads(s) for s in (args.folder / "trials.jsonl").read_text().splitlines()
    ]
    groups = {}
    for phase in sorted({r["phase"] for r in rows}):
        groups[phase] = {}
        for profile in sorted({r["profile"] for r in rows if r["phase"] == phase}):
            groups[phase][profile] = summarize(
                [r for r in rows if r["phase"] == phase and r["profile"] == profile]
            )
    diagnostics = [
        {
            key: row.get(key)
            for key in (
                "profile",
                "phase",
                "text_index",
                "seed",
                "stop_reason",
                "frames",
                "cap",
                "pcm",
                "raw",
                "edit_distance",
                "eos_steps",
            )
        }
        for row in rows
        if row["phase"] == "diagnostic"
        and row["text_index"] == 0
        and row["seed"] in (16, 25)
    ]
    result = {"groups": groups, "known_seed_diagnostics": diagnostics}
    (args.folder / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    print(json.dumps(groups, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
