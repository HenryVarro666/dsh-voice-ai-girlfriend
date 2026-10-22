#!/usr/bin/env python3
"""Recompute browser playback and full DSH timing tables from saved events."""

import argparse
import json
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    args = parser.parse_args()
    data = args.folder / "data"
    raw = json.loads((data / "browser-e2e.json").read_text())
    stages = []
    for row in raw:
        if "error" in row:
            stages.append({"error": row["error"]})
            continue
        tts = min(e["at"] for e in row["events"] if e["api"] == "tts/stream")
        eligible = [
            e
            for e in row["events"]
            if e["api"] in ["stt", "turn"]
            and (e["api"] == "stt" or e.get("result", {}).get("complete"))
            and e.get("done", float("inf")) <= tts
        ]
        ready = max(e["done"] for e in eligible)
        stage = {
            "key": row["key"],
            "policy": row["policy"],
            "warmup": row["warmup"],
            "path": "fallback"
            if any(e["api"] == "stt" for e in row["events"])
            else "fast",
            "endpoint_stt_ms": ready - row["input_end"],
            "dsh_llm_ms": tts - ready,
            "tts_playback_ms": row["first_voice_at"] - tts,
            "total_ms": row["voice_to_voice_ms"],
            "sources": len(row["sources"]),
        }
        assert (
            abs(
                stage["endpoint_stt_ms"]
                + stage["dsh_llm_ms"]
                + stage["tts_playback_ms"]
                - stage["total_ms"]
            )
            < 0.01
        )
        assert all(s.get("ended_at") for s in row["sources"])
        stages.append(stage)
    groups = {}
    for policy in ["baseline", "guard3"]:
        for path in ["fast", "fallback", "all"]:
            values = [
                r["total_ms"]
                for r in stages
                if not r.get("warmup")
                and r.get("policy") == policy
                and (path == "all" or r["path"] == path)
            ]
            groups[f"{policy}-{path}"] = {
                "n": len(values),
                "median_ms": statistics.median(values),
                "min_ms": min(values),
                "max_ms": max(values),
            }
    player = json.loads((data / "browser-player.json").read_text())
    playback = []
    for row in player:
        assert "error" not in row
        playback.append(
            {
                "key": row["key"],
                "policy": row["policy"],
                "first_voice_ms": row["first_voice_at"] - row["started"]
                if row["first_voice_at"]
                else None,
                "last_ended_ms": row["last_ended_at"] - row["started"],
                "sources": len(row["sources"]),
                "post_stop_new_sources": sum(
                    s["called_at"] > row.get("stop_requested_at", float("inf"))
                    for s in row["sources"]
                ),
                "speaking_after_stop": row.get("speaking_after_stop"),
            }
        )
    weak = json.loads((data / "weak_checks.json").read_text())
    summary = {
        "e2e_n": len(raw),
        "e2e_errors": sum("error" in r for r in raw),
        "groups": groups,
        "first_session_ms": stages[0]["total_ms"],
        "player_n": len(player),
        "weak_voice_loss_cases": {
            p: sum(r["known_voice_samples_lost"] > 0 for r in weak if r["policy"] == p)
            for p in ["baseline", "guard99", "guard3"]
        },
        "stages": stages,
        "playback": playback,
        "human_review": "pending",
    }
    (args.folder / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    lines = [
        "# 浏览器原始事件可重算指标",
        "",
        "起点为最后一个有声输入采样，终点为第一个有声输出采样的AudioContext排定时刻；不含硬件。",
        "",
        "| 轮次 | 候选 | 路径 | 断轮/STT ms | DSH/LLM ms | TTS/播放 ms | 总计 ms |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for r in stages:
        lines.append(
            f"| {r['key']} | {r['policy']} | {r['path']} | {r['endpoint_stt_ms']:.1f} | {r['dsh_llm_ms']:.1f} | {r['tts_playback_ms']:.1f} | {r['total_ms']:.1f} |"
        )
    lines += [
        "",
        "## 分路径汇总",
        "",
        "| 组别 | n | 中位数 ms | 最小 ms | 最大 ms |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, r in groups.items():
        lines.append(
            f"| {name} | {r['n']} | {r['median_ms']:.1f} | {r['min_ms']:.1f} | {r['max_ms']:.1f} |"
        )
    lines += [
        "",
        "all混合两类等待路径，其联合中位数可能与各路径的比较方向不同；不能据此声称保护器使首声加速。样本少，不提供稳定p95。",
    ]
    (args.folder / "METRICS.md").write_text("\n".join(lines) + "\n")
    plt.rcParams.update(
        {"font.size": 9, "axes.spines.top": False, "axes.spines.right": False}
    )
    fig, ax = plt.subplots(figsize=(11, 7.5), layout="constrained")
    left = [0.0] * len(stages)
    for key, color, label in [
        ("endpoint_stt_ms", "#5271d5", "Endpoint + ASR"),
        ("dsh_llm_ms", "#e6973b", "DSH + first LLM clause"),
        ("tts_playback_ms", "#29a18d", "TTS + scheduled playback"),
    ]:
        values = [r[key] / 1000 for r in stages]
        ax.barh(range(len(stages)), values, left=left, color=color, label=label)
        left = [a + b for a, b in zip(left, values)]
    labels = [
        (
            "First session"
            if r["warmup"]
            else r["key"].replace("case-", "c").replace("-repeat-", " / r")
        )
        + " / "
        + r["policy"]
        for r in stages
    ]
    ax.set(
        yticks=list(range(len(stages))),
        yticklabels=labels,
        xlabel="Speech end to first scheduled voiced sample (seconds)",
        title="17 real DSH browser turns; synthetic microphone input",
    )
    ax.invert_yaxis()
    ax.legend(loc="lower right", frameon=False)
    for i, total in enumerate(left):
        ax.text(total + 0.03, i, f"{total:.2f}", va="center", fontsize=8)
    ax.set_xlim(0, max(left) + 0.45)
    for suffix in ["svg", "png"]:
        fig.savefig(args.folder / f"e2e-breakdown.{suffix}", dpi=160)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(9, 3), layout="constrained")
    ax.barh(0, summary["first_session_ms"] / 1000, color="#5271d5", height=0.35)
    for y, path in [(1, "fast"), (2, "fallback")]:
        for offset, policy, color in [
            (-0.18, "baseline", "#5271d5"),
            (0.18, "guard3", "#e6973b"),
        ]:
            value = groups[f"{policy}-{path}"]["median_ms"] / 1000
            ax.barh(
                y + offset,
                value,
                height=0.3,
                color=color,
                label=("Baseline" if policy == "baseline" else "Experimental guard3")
                if y == 1
                else None,
            )
            ax.text(value + 0.04, y + offset, f"{value:.2f}s", va="center", fontsize=9)
    ax.text(
        summary["first_session_ms"] / 1000 + 0.04,
        0,
        f"{summary['first_session_ms'] / 1000:.2f}s",
        va="center",
        fontsize=9,
    )
    ax.set(
        yticks=[0, 1, 2],
        yticklabels=[
            "First request (n=1)",
            "Fast path (n=4 per policy)",
            "Fallback (n=4 per policy)",
        ],
        xlabel="Speech end to first scheduled voiced sample (seconds)",
        xlim=(0, 4.4),
        title="17 DSH browser turns; synthetic input, hardware latency excluded",
    )
    ax.invert_yaxis()
    ax.legend(loc="lower right", frameon=False)
    for suffix in ["svg", "png"]:
        fig.savefig(args.folder / f"latency-summary.{suffix}", dpi=160)
    plt.close(fig)
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k not in {"stages", "playback"}},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
