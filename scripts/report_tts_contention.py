#!/usr/bin/env python3
"""Summarize the archived contention experiment without models or a GPU."""

import argparse
import json
import random
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

CONDITIONS = ["unloaded", "resident", "decode"]
LABELS = ["LLM absent", "LLM resident", "LLM decoding"]
COLORS = ["#4378b9", "#20a29a", "#a179c9"]


def describe(rows):
    latency = [r["first_voiced_received_ms"] for r in rows]
    rtf = [r["total_ms"] / (1000 * r["audio_s"]) for r in rows]
    return {
        "n": len(rows),
        "first_voiced_median_ms": round(statistics.median(latency), 2),
        "first_voiced_mean_ms": round(statistics.mean(latency), 2),
        "first_voiced_min_ms": min(latency),
        "first_voiced_max_ms": max(latency),
        "over_1000_ms": sum(t > 1000 for t in latency),
        "rtf_median": round(statistics.median(rtf), 4),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    rows = [
        json.loads(line)
        for line in (args.data / "trials.jsonl").read_text().splitlines()
    ]
    errors = [r for r in rows if r.get("error")]
    invalid = [
        r
        for r in rows
        if not r.get("error")
        and r["condition"] == "decode"
        and not r.get("decode_spans_first_audio")
    ]
    usable = [r for r in rows if r not in errors and r not in invalid]
    groups = {c: [r for r in usable if r["condition"] == c] for c in CONDITIONS}
    if not all(groups.values()):
        raise ValueError("All three conditions require valid observations")
    summary = {c: describe(groups[c]) for c in CONDITIONS}
    summary["attempts"] = len(rows)
    summary["errors"] = len(errors)
    summary["decode_overlap_invalid"] = len(invalid)
    summary["by_text"] = {}
    for index in range(4):
        summary["by_text"][index] = {
            c: describe([r for r in groups[c] if r["text_index"] == index])
            for c in CONDITIONS
        }
    summary["unloaded_phases"] = {}
    for phase in ["before", "after"]:
        group = [r for r in groups["unloaded"] if r["phase"] == phase]
        if group:
            summary["unloaded_phases"][phase] = describe(group)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    lines = [
        "# Numeric TTS contention results",
        "",
        f"Attempts: {len(rows)}. Errors: {len(errors)}. Invalid decode overlap: {len(invalid)}.",
        "Warmups are excluded. Units measure HTTP receipt, not browser playback or voice-to-voice latency.",
        "",
        "| Condition | n | Median first voiced PCM | Mean | Min | Max | >1 s | Median RTF |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for c in CONDITIONS:
        s = summary[c]
        lines.append(
            f"| {c} | {s['n']} | {s['first_voiced_median_ms']:.1f} ms | "
            f"{s['first_voiced_mean_ms']:.1f} ms | {s['first_voiced_min_ms']:.1f} ms | "
            f"{s['first_voiced_max_ms']:.1f} ms | {s['over_1000_ms']} | {s['rtf_median']:.3f} |"
        )
    lines += [
        "",
        "## Median first voiced PCM by text",
        "",
        "Text IDs refer to manifest.json; each cell uses the valid repetitions for that text and condition.",
        "",
        "| Text ID | LLM absent | Resident | Decoding | Decoding minus resident |",
        "|---|---:|---:|---:|---:|",
    ]
    for i, values in summary["by_text"].items():
        med = [values[c]["first_voiced_median_ms"] for c in CONDITIONS]
        lines.append(
            f"| {i} | {med[0]:.1f} ms | {med[1]:.1f} ms | {med[2]:.1f} ms | {med[2] - med[1]:+.1f} ms |"
        )
    lines += [
        "",
        "No stable p95 claim. Four texts and stochastic synthesis limit generalization.",
        "",
    ]
    (args.out / "METRICS.md").write_text("\n".join(lines))

    plt.rcParams.update({"font.family": "DejaVu Sans", "svg.fonttype": "none"})
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8))
    fig.patch.set_facecolor("#f8fafc")
    rng = random.Random(42)
    for ax, metric in zip(axes, ["latency", "rtf"]):
        ax.set_facecolor("#f8fafc")
        for i, c in enumerate(CONDITIONS):
            values = [
                r["first_voiced_received_ms"]
                if metric == "latency"
                else r["total_ms"] / (1000 * r["audio_s"])
                for r in groups[c]
            ]
            ax.scatter(
                [i + rng.uniform(-0.13, 0.13) for _ in values],
                values,
                color=COLORS[i],
                alpha=0.7,
                s=32,
            )
            median = statistics.median(values)
            ax.plot([i - 0.24, i + 0.24], [median, median], color=COLORS[i], lw=3)
            ax.annotate(
                f"median {median:.0f} ms"
                if metric == "latency"
                else f"median {median:.3f}",
                (i, median),
                xytext=(0, 9),
                textcoords="offset points",
                ha="center",
                fontsize=9,
            )
        ax.set_xticks(range(3), LABELS)
        ax.set_ylim(bottom=0, top=ax.get_ylim()[1] * 1.2)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", alpha=0.15)
        ax.set_axisbelow(True)
    axes[0].set_ylabel("TTS request to first voiced PCM received (ms)")
    axes[0].set_yscale("log")
    axes[0].set_ylim(80, 2600)
    axes[0].set_yticks(
        [100, 200, 400, 800, 1600, 2400], ["100", "200", "400", "800", "1600", "2400"]
    )
    axes[1].set_ylabel("Synthesis wall time / audio duration (RTF)")
    axes[0].set_title("First audio availability (log scale)")
    axes[1].set_title("Generation throughput")
    fig.suptitle(
        "Does concurrent LLM decoding slow streaming TTS?", fontsize=17, weight="bold"
    )
    counts = "/".join(str(len(groups[c])) for c in CONDITIONS)
    fig.text(
        0.02,
        0.02,
        f"RTX 5080 / Qwen3.5 9B + Qwen3-TTS 0.6B. Trials: {counts}. HTTP timing only; no p95 claim.",
        fontsize=9,
        color="#506178",
    )
    fig.tight_layout(rect=(0, 0.05, 1, 0.96))
    for ext in ["png", "svg"]:
        fig.savefig(
            args.out / f"contention.{ext}", dpi=150, facecolor=fig.get_facecolor()
        )
    plt.close(fig)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
