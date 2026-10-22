#!/usr/bin/env python3
"""Recompute generation-layout tables and figures from archived numbers."""

import argparse
import json
import statistics
from pathlib import Path

from report_generation_end import median, summarize

PROFILES = ("baseline", "text_step", "xvector", "chunk2")
LABELS = ("Full text / ICL / chunk 4", "Step text", "Speaker vector only", "Chunk 2")


def grouped_summary(rows):
    groups = {}
    for phase in ("diagnostic", "holdout"):
        groups[phase] = {}
        pairs = {}
        for row in rows:
            if row["phase"] == phase:
                pairs.setdefault((row["text_index"], row["seed"]), {})[
                    row["profile"]
                ] = row
        for profile in PROFILES:
            selected = [
                r for r in rows if r["phase"] == phase and r["profile"] == profile
            ]
            valid = [r for r in selected if "error" not in r]
            result = summarize(selected)
            leading = [
                r["raw"]["leading_ms"]
                for r in valid
                if r["raw"]["leading_ms"] is not None
            ]
            result.update(
                raw_first_median_ms=median(valid, "raw_first_ms"),
                raw_first_voiced_median_ms=median(valid, "raw_first_voiced_ms"),
                first_voiced_max_ms=max(
                    (
                        r["first_voiced_ms"]
                        for r in valid
                        if r["first_voiced_ms"] is not None
                    ),
                    default=None,
                ),
                raw_leading_median_ms=statistics.median(leading) if leading else None,
                raw_leading_max_ms=max(leading, default=None),
                long_leading=sum(v > 1000 for v in leading),
                frames_total=sum(r["frames"] for r in valid),
                paired={
                    "complete": 0,
                    "same_codes": 0,
                    "edits_better": 0,
                    "edits_worse": 0,
                    "edits_equal": 0,
                },
            )
            for pair in pairs.values():
                base, candidate = pair.get("baseline"), pair.get(profile)
                if not base or not candidate or "error" in base or "error" in candidate:
                    continue
                result["paired"]["complete"] += 1
                result["paired"]["same_codes"] += (
                    base["code_sha256"] == candidate["code_sha256"]
                )
                delta = candidate["edit_distance"] - base["edit_distance"]
                result["paired"][
                    "edits_better"
                    if delta < 0
                    else "edits_worse"
                    if delta > 0
                    else "edits_equal"
                ] += 1
            groups[phase][profile] = result
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True, type=Path)
    args = parser.parse_args()
    rows = [
        json.loads(s) for s in (args.folder / "trials.jsonl").read_text().splitlines()
    ]
    manifest = json.loads((args.folder / "manifest.json").read_text())
    expected = {
        (phase, profile, index, seed)
        for phase in ("diagnostic", "holdout")
        for profile in PROFILES
        for index in range(len(manifest[phase]["texts"]))
        for seed in manifest[phase]["seeds"]
    }
    actual = {(r["phase"], r["profile"], r["text_index"], r["seed"]) for r in rows}
    if actual != expected or len(rows) != len(expected):
        raise ValueError(
            "Incomplete or duplicated grid; retain raw data, do not label plots complete"
        )
    groups = grouped_summary(rows)
    (args.folder / "summary.json").write_text(
        json.dumps(groups, ensure_ascii=False, indent=2)
    )
    report = [
        "# 可重算生成布局指标",
        "",
        "计时到handler有声PCM可用；不含HTTP、LLM、浏览器。ASR不等于听感验收。",
        "",
    ]
    for phase, profiles in groups.items():
        report.extend(
            [
                f"## {phase}",
                "",
                "| 策略 | n / 错误 | EOS / 上限 | 首有声中位 / 最大ms | 总生成中位ms | 前导>1s | 尾部>1s / 最大ms | ASR匹配 / 编辑数 | 同基线码序列 |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for profile, r in profiles.items():

            def number(key, row=r):
                return f"{row[key]:.1f}" if row[key] is not None else "null"

            report.append(
                f"| {profile} | {r['trials']} / {r['errors']} | {r['eos']} / {r['max_tokens']} | {number('first_voiced_median_ms')} / {number('first_voiced_max_ms')} | {number('generation_median_ms')} | {r['long_leading']} | {r['long_tails']} / {number('tail_max_ms')} | {r['asr_exact']} / {r['edit_distance']} | {r['paired']['same_codes']} / {r['paired']['complete']} |"
            )
        report.append("")
    (args.folder.parent / "METRICS.md").write_text("\n".join(report))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.size": 10, "svg.fonttype": "none"})
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), constrained_layout=True)
    for ax, phase, title in zip(
        axes,
        ("diagnostic", "holdout"),
        ("Known-seed diagnosis (8 per policy)", "New texts and seeds (12 per policy)"),
    ):
        for index, profile in enumerate(PROFILES):
            values = sorted(
                r["first_voiced_ms"]
                for r in rows
                if r["phase"] == phase
                and r["profile"] == profile
                and "error" not in r
                and r.get("first_voiced_ms") is not None
            )
            offset = [(i - (len(values) - 1) / 2) * 0.018 for i in range(len(values))]
            ax.scatter(
                values, [index + j for j in offset], s=22, alpha=0.65, color="#35709f"
            )
            if values:
                ax.scatter(
                    [statistics.median(values)],
                    [index],
                    marker="|",
                    s=240,
                    color="#bd4b30",
                    linewidths=2.4,
                )
        ax.set_yticks(range(4), LABELS)
        ax.invert_yaxis()
        ax.set_title(title)
        ax.set_xlabel("Handler first voiced PCM available (ms)")
        ax.grid(axis="x", alpha=0.2)
    fig.suptitle(
        "Generation layout: every output shown; orange = median\nNo added audio cropping; no HTTP / LLM / browser"
    )
    for suffix in ("svg", "png"):
        fig.savefig(args.folder.parent / f"first-voice.{suffix}", dpi=160)
    plt.close(fig)
    print(json.dumps(groups, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
