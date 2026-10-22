#!/usr/bin/env python3
"""Recompute fixed-stream splitting and isolated silence-trace results."""

import argparse
import json
import statistics
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def read_rows(path):
    return [
        json.loads(line) for line in (path / "trials.jsonl").read_text().splitlines()
    ]


def save_figure(fig, out, name):
    fig.tight_layout()
    for ext in ["png", "svg"]:
        fig.savefig(out / f"{name}.{ext}", dpi=150, facecolor="#f8fafc")
    plt.close(fig)


def tail_report(data):
    rows = read_rows(data)
    main = [r for r in rows if r["phase"] == "main"]
    replay = [r for r in rows if r["phase"] == "replay"]
    summary = {
        "n": len(main),
        "replays": len(replay),
        "raw_first_median_ms": statistics.median(r["first_raw_ms"] for r in main),
        "handler_first_median_ms": statistics.median(
            r["first_handler_ms"] for r in main
        ),
        "handler_first_max_ms": max(r["first_handler_ms"] for r in main),
        "over_1000_ms": sum(r["first_handler_ms"] > 1000 for r in main),
        "max_raw_leading_silence_ms": max(r["raw_leading_silence_ms"] for r in main),
    }
    out = data.parent
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    lines = [
        "# Isolated TTS stage metrics",
        "",
        f"Main seeds: {len(main)}; separate replays: {len(replay)}.",
        "",
        "| Seed | Phase | First raw block | First voiced raw block | First handler PCM | Raw leading silence |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['seed']} | {r['phase']} | {r['first_raw_ms']:.2f} ms | {r['first_raw_voiced_ms']:.2f} ms | {r['first_handler_ms']:.2f} ms | {r['raw_leading_silence_ms']:.2f} ms |"
        )
    (out / "METRICS.md").write_text("\n".join(lines) + "\n")
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    axes[0].scatter(
        [r["raw_leading_silence_ms"] / 1000 for r in main],
        [r["first_handler_ms"] for r in main],
        color="#237e7b",
    )
    axes[0].set(
        xlabel="Generated leading silence (audio seconds)",
        ylabel="First handler PCM (wall ms)",
        title="Silence generation delays usable output",
    )
    slow = max(main, key=lambda r: r["first_handler_ms"])
    labels = ["First raw audio", "First voiced raw audio", "First handler PCM"]
    values = [
        slow["first_raw_ms"],
        slow["first_raw_voiced_ms"],
        slow["first_handler_ms"],
    ]
    axes[1].barh(labels, values, color=["#4378b9", "#a179c9", "#237e7b"])
    axes[1].invert_yaxis()
    for i, v in enumerate(values):
        axes[1].text(v + 15, i, f"{v:.0f} ms", va="center")
    axes[1].set(
        xlim=(0, max(values) * 1.2),
        xlabel="Wall time from synthesis start (ms)",
        title=f"Reproducible slow seed {slow['seed']}",
    )
    fig.suptitle(
        "Isolated TTS: raw audio is not yet usable speech", weight="bold", fontsize=15
    )
    for ax in axes:
        ax.grid(axis="x", alpha=0.15)
        ax.spines[["top", "right"]].set_visible(False)
    save_figure(fig, out, "tail-stages")
    print("TAIL", json.dumps(summary))


def clause_report(data):
    rows = read_rows(data)
    groups = {
        p: [r for r in rows if r["policy"] == p]
        for p in ["current_5", "later_18", "sentence"]
    }
    fields = [
        "first_text_ready_ms",
        "first_voiced_received_ms",
        "ideal_first_voice_ms",
        "synthesis_ms",
        "underrun_ms",
        "audio_s",
    ]
    summary = {
        p: {"n": len(rs), **{f: statistics.median(r[f] for r in rs) for f in fields}}
        for p, rs in groups.items()
    }
    summary["tts_requests"] = sum(len(r["segments"]) for r in rows)
    summary["by_text"] = {
        str(i): {
            p: statistics.median(
                r["first_voiced_received_ms"] for r in rs if r["text_index"] == i
            )
            for p, rs in groups.items()
        }
        for i in range(4)
    }
    out = data.parent
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    lines = [
        "# First-clause replay metrics",
        "",
        "Time zero is the first content packet of a recorded copy task. No ASR, DSH prefill or real browser playback.",
        "",
        "| Policy | n | Text ready median | First voiced PCM median | Ideal first voice estimate | Full synthesis median | Ideal starvation median |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for p in groups:
        s = summary[p]
        lines.append(
            f"| {p} | {s['n']} | {s['first_text_ready_ms']:.1f} ms | {s['first_voiced_received_ms']:.1f} ms | {s['ideal_first_voice_ms']:.1f} ms | {s['synthesis_ms']:.1f} ms | {s['underrun_ms']:.1f} ms |"
        )
    lines += [
        "",
        "## First voiced PCM medians by fixed answer",
        "",
        "| Text ID | current_5 | later_18 | sentence |",
        "|---|---:|---:|---:|",
    ]
    for i, stats in summary["by_text"].items():
        lines.append(
            f"| {i} | " + " | ".join(f"{stats[p]:.1f} ms" for p in groups) + " |"
        )
    (out / "METRICS.md").write_text("\n".join(lines) + "\n")
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    labels = ["Current first clause", "Later first clause", "Whole first sentence"]
    colors = ["#4378b9", "#a179c9", "#237e7b"]
    for i, (p, rs) in enumerate(groups.items()):
        values = [r["first_voiced_received_ms"] for r in rs]
        axes[0].scatter(
            [i + (j % 3 - 1) * 0.07 for j in range(len(values))],
            values,
            color=colors[i],
            alpha=0.7,
        )
        med = statistics.median(values)
        axes[0].plot([i - 0.2, i + 0.2], [med, med], color=colors[i], lw=3)
        axes[0].annotate(
            f"{med:.0f} ms",
            (i, med),
            xytext=(0, 12),
            textcoords="offset points",
            ha="center",
        )
    axes[0].set_xticks(range(3), labels, fontsize=8)
    axes[0].set(
        ylabel="First content packet to voiced PCM received (ms)",
        title="Recorded-stream replay, 12 trials per policy",
    )
    for i, p in enumerate(groups):
        axes[1].plot(
            range(4),
            [summary["by_text"][str(j)][p] for j in range(4)],
            marker="o",
            color=colors[i],
            label=labels[i],
        )
    axes[1].set(
        xlabel="Fixed answer ID",
        ylabel="Median first voiced PCM (ms)",
        title="Three repetitions per cell",
    )
    axes[1].set_xticks(range(4))
    axes[1].legend(fontsize=8)
    fig.suptitle(
        "Does a different first-clause boundary help?", fontsize=15, weight="bold"
    )
    for ax in axes:
        ax.grid(axis="y", alpha=0.15)
        ax.spines[["top", "right"]].set_visible(False)
    save_figure(fig, out, "first-clause")
    print("CLAUSE", json.dumps(summary))


def crop_report(clause, tail):
    paths = [(clause, "reply"), (tail, "seed")]
    affected = []
    for path, kind in paths:
        rows = json.loads((path / "crop_results.json").read_text())
        for row in rows:
            if row["removed_ms"]:
                label = f"{kind} {row.get('trial_id', row.get('seed'))}"
                affected.append((label, row))
    fig, ax = plt.subplots(figsize=(9, 4))
    at = np.arange(len(affected))
    ax.barh(
        at - 0.18,
        [r["original_audio_ms"] / 1000 for _, r in affected],
        height=0.34,
        color="#a179c9",
        label="Original generated audio",
    )
    ax.barh(
        at + 0.18,
        [r["cropped_audio_ms"] / 1000 for _, r in affected],
        height=0.34,
        color="#237e7b",
        label="Offline crop candidate",
    )
    ax.set_yticks(at, [label for label, _ in affected])
    ax.invert_yaxis()
    ax.set_xlabel("Audio seconds, affected recordings only")
    ax.set_title(
        "Long low-amplitude tails can dominate playback duration", weight="bold"
    )
    ax.legend(fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="x", alpha=0.15)
    save_figure(fig, tail.parent, "tail-crop")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tail", type=Path)
    parser.add_argument("--clause", type=Path)
    args = parser.parse_args()
    plt.rcParams.update({"font.family": "DejaVu Sans", "svg.fonttype": "none"})
    if args.tail:
        tail_report(args.tail)
    if args.clause:
        clause_report(args.clause)
    if args.clause and args.tail:
        crop_report(args.clause, args.tail)


if __name__ == "__main__":
    main()
