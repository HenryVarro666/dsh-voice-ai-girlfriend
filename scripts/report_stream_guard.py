#!/usr/bin/env python3
"""Regenerate model-comparison tables and figures from archived numbers."""

import argparse
import json
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True, type=Path)
    args = parser.parse_args()
    summaries, trials = {}, {}
    for name in ["0.6b", "1.7b", "holdout-0.6b", "holdout-1.7b"]:
        folder = args.folder / "data" / name
        rows = [
            json.loads(line)
            for line in (folder / "trials.jsonl").read_text().splitlines()
        ]
        manifest = json.loads((folder / "manifest.json").read_text())
        checks = json.loads((folder / "asr_check.json").read_text())
        original_checks = [r for r in checks if r["kind"] == "original"]
        summaries[name] = {
            "n": len(rows),
            "first_voiced_median_ms": statistics.median(
                r["original"]["first_voiced_ms"] for r in rows
            ),
            "first_voiced_max_ms": max(r["original"]["first_voiced_ms"] for r in rows),
            "rtf_median": statistics.median(
                r["total_ms"] / r["original"]["audio_ms"] for r in rows
            ),
            "tails_over_1s": sum(r["original"]["tail_ms"] > 1000 for r in rows),
            "added_gpu_mib": manifest["gpu_loaded"]["used_mib"]
            - manifest["gpu_before"]["used_mib"],
            "asr_exact": sum(r["matched"] for r in original_checks),
            "per_text_first_voiced_ms": [
                statistics.median(
                    r["original"]["first_voiced_ms"]
                    for r in rows
                    if r["text_index"] == i
                )
                for i in range(len(manifest["texts"]))
            ],
        }
        trials[name] = rows
    replays = {}
    for version in ["v1", "v2-final"]:
        rows = json.loads(
            (args.folder / "data" / f"replay-{version}" / "replay.json").read_text()
        )["records"]
        timed = [r for r in rows if r["packet_times_available"]]
        replays[version] = {
            "n": len(rows),
            "timed_n": len(timed),
            "segments": sum(len(r["segments"]) for r in rows),
            "first_voice_delay_max_ms": max(r["first_voice_delay_ms"] for r in timed),
            "added_underrun_max_ms": max(r["added_underrun_ms"] for r in timed),
            "peak_pending_bytes": max(
                s["peak_pending_bytes"] for r in rows for s in r["segments"]
            ),
            "affected": [
                {"file": r["file"], "removed_ms": r["removed_ms"]}
                for r in rows
                if r["removed_ms"]
            ],
        }
    (args.folder / "summary.json").write_text(
        json.dumps({"models": summaries, "replays": replays}, indent=2)
    )
    lines = [
        "# 可重算模型数值",
        "",
        "计时为独立进程调用 handler 到首个有声 PCM 可用；不含 HTTP、LLM、DSH 或实际播放。",
        "",
        "| 组别 | 条数 | 首有声中位数 ms | 最大值 ms | 生成时间/音频时长 | 增量显存 MiB | 原音频ASR精确匹配 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, values in summaries.items():
        lines.append(
            f"| {name} | {values['n']} | {values['first_voiced_median_ms']:.1f} | {values['first_voiced_max_ms']:.1f} | {values['rtf_median']:.3f} | {values['added_gpu_mib']} | {values['asr_exact']}/{values['n']} |"
        )
    lines += [
        "",
        "初始12条含已知诊断反例；新文本40条分别报告，不能混合估计生产长尾概率。显存为整卡加载后减加载前快照，包含该进程CUDA图等开销。",
        "",
        "## 新文本每条的首声中位数",
        "",
        "| 文本编号 | 0.6B ms | 1.7B ms |",
        "|---|---:|---:|",
    ]
    for i, (small, large) in enumerate(
        zip(
            summaries["holdout-0.6b"]["per_text_first_voiced_ms"],
            summaries["holdout-1.7b"]["per_text_first_voiced_ms"],
        )
    ):
        lines.append(f"| {i} | {small:.1f} | {large:.1f} |")
    (args.folder / "METRICS.md").write_text("\n".join(lines) + "\n")
    plt.rcParams.update(
        {"font.size": 10, "axes.spines.top": False, "axes.spines.right": False}
    )
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), layout="constrained")
    colors = ["#2563eb", "#e87935"]
    for offset, model in enumerate(["0.6b", "1.7b"]):
        for phase in range(2):
            key = model if phase == 0 else f"holdout-{model}"
            values = [r["original"]["first_voiced_ms"] for r in trials[key]]
            x = phase * 3 + offset
            axes[0].scatter(
                [x] * len(values), values, color=colors[offset], alpha=0.45, s=25
            )
            axes[0].plot(
                [x - 0.18, x + 0.18],
                [statistics.median(values)] * 2,
                color="black",
                linewidth=2,
            )
    axes[0].set(
        xticks=[0.5, 3.5],
        xticklabels=[
            "Diagnostic seeds\n12 per model",
            "New texts + seeds\n40 per model",
        ],
        ylabel="First voiced PCM available (ms)",
        title="Raw trials; black line = median",
    )
    axes[0].text(
        0.98,
        0.97,
        "Blue: 0.6B   Orange: 1.7B",
        transform=axes[0].transAxes,
        va="top",
        ha="right",
    )
    indices = list(range(8))
    for model, color in zip(["0.6b", "1.7b"], colors):
        axes[1].plot(
            indices,
            summaries[f"holdout-{model}"]["per_text_first_voiced_ms"],
            "o-",
            color=color,
            label=model.upper(),
        )
    axes[1].set(
        xlabel="New text index (5 seeds each)",
        ylabel="Median first voiced PCM (ms)",
        title="1.7B gains were text dependent",
        ylim=(80, 210),
        xticks=indices,
    )
    axes[1].legend(frameon=False)
    fig.suptitle("Same voice and engine on RTX 5080; isolated TTS, no LLM", fontsize=13)
    for suffix in ["svg", "png"]:
        fig.savefig(args.folder / f"model-comparison.{suffix}", dpi=170)
    plt.close(fig)
    print(json.dumps({"models": summaries, "replays": replays}, indent=2))


if __name__ == "__main__":
    main()
