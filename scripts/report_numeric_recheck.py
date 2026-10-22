#!/usr/bin/env python3
"""Recompute frozen per-audio checks and paired timing without model inference."""

import argparse
import json
from collections import Counter
from pathlib import Path

from recheck_metrics import repeated_summary
from report_generation_end import summarize


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def aggregate(items):
    """Count audio files, never treat repeat calls as independent generations."""
    result = {
        "audios": len(items),
        "calls": sum(x["calls"] for x in items),
        "errors": sum(x["errors"] for x in items),
        "missing": sum(x["missing"] for x in items),
        "stable_exact": sum(x["stable_exact"] for x in items),
        "variable_transcripts": sum(x["variants"] > 1 for x in items),
    }
    if items and "stable_fields" in items[0]:
        result.update(
            stable_fields=sum(x["stable_fields"] for x in items),
            any_fields_match=sum(x["any_fields_match"] for x in items),
            variable_field_results=sum(x["field_result_changes"] for x in items),
        )
    return result


def paired_outcomes(suite, checks, field):
    pairs = {}
    for item in suite:
        key = (
            (item["phase"], item["text_index"], item["seed"])
            if "phase" in item
            else item["pair_id"]
        )
        profiles = pairs.setdefault(key, {})
        if item["profile"] in profiles:
            raise ValueError("Duplicate profile in an audio pair")
        profiles[item["profile"]] = checks[item["id"]][field]
    counts = Counter()
    for profiles in pairs.values():
        if set(profiles) != {"baseline", "xvector"}:
            raise ValueError("Incomplete default/vector pair")
        baseline, candidate = profiles["baseline"], profiles["xvector"]
        counts[
            "both_stable"
            if baseline and candidate
            else "candidate_only_stable"
            if candidate
            else "baseline_only_stable"
            if baseline
            else "neither_stable"
        ] += 1
    return {"pairs": len(pairs), "outcomes": dict(counts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True, type=Path)
    args = parser.parse_args()
    data = args.folder / "data"
    suites, checks = {}, {}
    for label in ("archived", "existing", "new"):
        suites[label] = json.loads((data / f"{label}-suite.json").read_text())
        checks[label] = repeated_summary(
            suites[label], read_jsonl(data / f"{label}-stt/repeats.jsonl")
        )
    rows = read_jsonl(data / "generation-trials.jsonl")
    cases = json.loads((args.folder / "cases.json").read_text())
    frozen = {
        f"{phase}-{profile}-t{index}-s{seed}"
        for phase in ("diagnostic", "holdout")
        for profile in ("baseline", "xvector")
        for index in range(len(cases[phase]))
        for seed in cases[f"{phase}_seeds"]
    }
    expected = {item["id"] for item in suites["new"]}
    if (
        expected != frozen
        or len(rows) != len(expected)
        or {r["key"] for r in rows} != expected
    ):
        raise ValueError("Generation rows do not cover the frozen ASR suite once")
    result = {
        "scope": "isolated warm handler; first voiced PCM, not DSH or hardware",
        "archived": checks["archived"],
        "existing": {},
        "new": {},
        "per_audio": checks,
        "flagged_new": [],
    }
    for profile in ("baseline", "xvector"):
        result["existing"][profile] = aggregate(
            [
                checks["existing"][item["id"]]
                for item in suites["existing"]
                if item["profile"] == profile
            ]
        )
    result["existing"]["paired"] = paired_outcomes(
        suites["existing"], checks["existing"], "stable_exact"
    )
    lines = [
        "# 可重算自动对照指标",
        "",
        "每份PCM三次识别全匹配才计稳定；计数单位是音频。计时不含ASR、HTTP、DSH或硬件。",
        "",
        "| 分组 | 模式 | 音频 / EOS / 错误 | 三次字段全匹配 | 首有声中位 / 最大ms | 总生成中位ms | 段尾>1s / 最大ms |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for phase in ("diagnostic", "holdout"):
        result["new"][phase] = {}
        phase_suite = [x for x in suites["new"] if x["phase"] == phase]
        for profile in ("baseline", "xvector"):
            group = [r for r in rows if r["phase"] == phase and r["profile"] == profile]
            metrics = summarize(group)
            metrics["first_voiced_max_ms"] = max(
                (r["first_voiced_ms"] for r in group if r.get("first_voiced_ms")),
                default=None,
            )
            metrics["raw_leading_over_1s"] = sum(
                r.get("raw", {}).get("leading_ms", 0) > 1000 for r in group
            )
            metrics["unexpected_gpu_trials"] = sum(
                bool(r.get("unexpected_gpu_pids_after")) for r in group
            )
            metrics["repeated_asr"] = aggregate(
                [checks["new"][x["id"]] for x in phase_suite if x["profile"] == profile]
            )
            result["new"][phase][profile] = metrics
            lines.append(
                f"| {phase} | {profile} | {metrics['trials']} / {metrics['eos']} / {metrics['errors']} | "
                f"{metrics['repeated_asr']['stable_fields']} | "
                f"{metrics['first_voiced_median_ms']:.1f} / {metrics['first_voiced_max_ms']:.1f} | "
                f"{metrics['generation_median_ms']:.1f} | "
                f"{metrics['long_tails']} / {metrics['tail_max_ms']:.1f} |"
            )
        result["new"][phase]["paired"] = paired_outcomes(
            phase_suite, checks["new"], "stable_fields"
        )
    for item in suites["new"]:
        if not checks["new"][item["id"]]["stable_fields"]:
            result["flagged_new"].append(item | {"checks": checks["new"][item["id"]]})
    (data / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    (args.folder / "METRICS.md").write_text("\n".join(lines) + "\n")
    make_figure(args.folder, rows, result)
    print(json.dumps({k: result[k] for k in ("existing", "new")}, indent=2))


def make_figure(folder, rows, result):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"baseline": "#59779c", "xvector": "#d37843"}
    fig, axes = plt.subplots(2, 2, figsize=(11, 7.5), layout="constrained")
    holdout = [r for r in rows if r["phase"] == "holdout"]
    for ax, field, title in (
        (axes[0, 0], "first_voiced_ms", "First voiced PCM: 18 new-input pairs"),
        (axes[0, 1], "generation_ms", "Complete generation: 18 new-input pairs"),
    ):
        keys = sorted({(r["text_index"], r["seed"]) for r in holdout})
        for index, key in enumerate(keys):
            pair = {
                r["profile"]: r for r in holdout if (r["text_index"], r["seed"]) == key
            }
            ax.plot([0, 1], [pair[p][field] for p in colors], color="#c3c7cd", lw=0.8)
        for x, profile in enumerate(colors):
            values = [r[field] for r in holdout if r["profile"] == profile]
            ax.scatter([x] * len(values), values, c=colors[profile], s=24, alpha=0.8)
            metric = result["new"]["holdout"][profile][
                "first_voiced_median_ms"
                if field == "first_voiced_ms"
                else "generation_median_ms"
            ]
            ax.plot([x - 0.12, x + 0.12], [metric, metric], color="black", lw=2)
        ax.set(
            xticks=[0, 1],
            xticklabels=["Default ICL", "Vector only"],
            ylabel="Milliseconds",
            title=title,
            xlim=(-0.35, 1.35),
        )
        ax.grid(axis="y", alpha=0.2)
    ax = axes[1, 0]
    for x, phase in enumerate(("diagnostic", "holdout")):
        for offset, profile in ((-0.18, "baseline"), (0.18, "xvector")):
            group = result["new"][phase][profile]
            value = group["repeated_asr"]["stable_fields"]
            ax.bar(x + offset, value, 0.34, color=colors[profile])
            ax.text(x + offset, value + 0.3, f"{value}/{group['trials']}", ha="center")
    ax.set(
        xticks=[0, 1],
        xticklabels=["Known cases", "New inputs"],
        ylim=(0, 21),
        ylabel="Audio files",
        title="All fields match in all 3 ASR calls",
    )
    ax = axes[1, 1]
    for x, profile in enumerate(colors):
        values = [r["pcm"]["tail_ms"] for r in rows if r["profile"] == profile]
        ax.scatter([x] * len(values), values, c=colors[profile], s=24, alpha=0.7)
    ax.axhline(1000, ls="--", color="#a14848", lw=1, label="1 s diagnostic line")
    ax.legend(loc="upper right", frameon=False)
    ax.set(
        xticks=[0, 1],
        xticklabels=["Default ICL", "Vector only"],
        ylabel="Tail milliseconds (threshold 99)",
        title="All 24 outputs per mode; no added crop",
        xlim=(-0.35, 1.35),
    )
    ax.grid(axis="y", alpha=0.2)
    fig.suptitle(
        "apex Qwen3-TTS 0.6B | same text / seed | isolated warm handler", fontsize=13
    )
    for ext in ("svg", "png"):
        fig.savefig(folder / f"comparison.{ext}", dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    main()
