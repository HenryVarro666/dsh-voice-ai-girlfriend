#!/usr/bin/env python3
"""Recalculate historical turn metrics from numeric scores, without models."""

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCORES = ROOT / "docs/experiments/2026-10-19-turn/scores.json"
THRESHOLDS = {
    "Qwen3-0.6B-end": 0.2,
    "large": 0.99,
    "rt": 0.99,
    "zh": 0.99,
    "smart-turn-fp32": 0.5,
}


def auc(positive: list[float], negative: list[float]) -> float:
    """Rank AUC with average ranks for ties; no sklearn dependency needed."""
    ordered = sorted([(p, True) for p in positive] + [(p, False) for p in negative])
    rank_sum = 0.0
    start = 0
    while start < len(ordered):
        stop = start + 1
        while stop < len(ordered) and ordered[stop][0] == ordered[start][0]:
            stop += 1
        rank_sum += (
            (start + stop + 1) / 2 * sum(label for _, label in ordered[start:stop])
        )
        start = stop
    n = len(positive)
    return (rank_sum - n * (n + 1) / 2) / (n * len(negative))


def scores(rows: list[dict], tag: str, complete: bool) -> list[float]:
    return [r["scores"][tag] for r in rows if r["complete"] == complete]


def rate(values: list[float], threshold: float) -> float:
    return sum(p > threshold for p in values) / len(values)


def main() -> None:
    archive = json.loads(SCORES.read_text())
    datasets = defaultdict(list)
    for row in archive["rows"]:
        datasets[row["set"]].append(row)

    print("# Archived Chinese turn evaluation\n")
    print(
        f"Source: {archive['source_date']}; numeric export: {archive['export_date']}."
    )
    print("No model inference is performed; all decisions use score > threshold.\n")
    print("## Label counts\n")
    print("| Dataset | Complete | Incomplete / internal pause cuts |")
    print("|---|---:|---:|")
    for name, rows in datasets.items():
        complete = sum(r["complete"] for r in rows)
        print(f"| {name} | {complete} | {len(rows) - complete} |")

    print("\n## Classifier metrics\n")
    print(
        "TPR: complete accepted; FPR: incomplete accepted. Pauses are cut-level here.\n"
    )
    print("| Dataset | Judge | Threshold | AUC | TPR | FPR | Accuracy |")
    print("|---|---|---:|---:|---:|---:|---:|")
    for name, rows in datasets.items():
        for tag, threshold in THRESHOLDS.items():
            if any(tag not in r["scores"] for r in rows):
                continue
            positive, negative = scores(rows, tag, True), scores(rows, tag, False)
            tpr, fpr = rate(positive, threshold), rate(negative, threshold)
            accuracy = (tpr * len(positive) + (1 - fpr) * len(negative)) / len(rows)
            print(
                f"| {name} | {tag} | {threshold} | {auc(positive, negative):.3f} "
                f"| {tpr:.1%} | {fpr:.1%} | {accuracy:.2%} |"
            )

    print("\n## FLEURS timeout policy simulation\n")
    print("Wait excludes STT, scoring, network and playback time.")
    print("Cut rate uses all 409 complete source utterances as denominator, including")
    print(
        "ones with no archived internal pause. This is not a conversation benchmark.\n"
    )
    print("| Policy | Mean endpoint wait | Source utterances cut |")
    print("|---|---:|---:|")
    rows = datasets["fleurs"]
    finished = [r for r in rows if r["complete"]]
    pauses = [r for r in rows if not r["complete"]]
    policies = [(None, 1.8), (None, 1.2), (None, 1.0), ("Qwen3-0.6B-end", 1.2)]
    for tag, fallback in policies:

        def accepts(row: dict) -> bool:
            return tag is not None and row["scores"][tag] > THRESHOLDS[tag]

        wait = sum(0.5 if accepts(r) else fallback for r in finished) / len(finished)
        cut = {
            r["utt"] for r in pauses if r["pause_ms"] >= fallback * 1000 or accepts(r)
        }
        label = (
            f"{tag}, check 0.5 s / fallback {fallback} s"
            if tag
            else f"Fixed {fallback} s"
        )
        print(f"| {label} | {wait:.2f} s | {len(cut) / len(finished):.1%} |")


if __name__ == "__main__":
    main()
