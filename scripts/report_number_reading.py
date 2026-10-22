#!/usr/bin/env python3
"""Summarize structured numeric ASR evidence, not human listening judgments."""

import argparse
import base64
import html
import json
from collections import Counter
from pathlib import Path

from number_reading import check
from report_generation_end import summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(s) for s in (args.run / "trials.jsonl").read_text().splitlines()]
    manifest = json.loads((args.run / "manifest.json").read_text())
    transcripts = {
        r["key"]: r
        for r in (
            json.loads(s)
            for s in (args.run / "transcripts.jsonl").read_text().splitlines()
        )
    }
    for row in rows:
        key = f"numbers-{row['profile']}-t{row['text_index']}-s{row['seed']}"
        if key in transcripts:
            row["number_check"] = check(
                manifest["cases"][row["text_index"]], transcripts[key]["heard"]
            )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "checked_trials.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    )
    result = {"groups": {}, "cases": [], "persistent_normalized_issues": []}
    for profile in ("raw", "normalized"):
        selected = [r for r in rows if r["profile"] == profile]
        group = summarize(selected)
        group["numeric_status"] = dict(
            Counter(r.get("number_check", {}).get("status", "error") for r in selected)
        )
        group["fields"] = {
            field: dict(
                Counter(
                    r["number_check"]["fields"][field]
                    for r in selected
                    if field in r.get("number_check", {}).get("fields", {})
                )
            )
            for field in ("code", "date", "time")
        }
        result["groups"][profile] = group
    for index, scenario in enumerate(manifest["cases"]):
        entry = {"index": index, "expected": scenario, "profiles": {}}
        for profile in ("raw", "normalized"):
            selected = [
                r for r in rows if r["profile"] == profile and r["text_index"] == index
            ]
            entry["profiles"][profile] = [
                {
                    "seed": r["seed"],
                    "check": r.get("number_check"),
                    "error": r.get("error"),
                }
                for r in selected
            ]
        result["cases"].append(entry)
        selected = [
            r for r in rows if r["profile"] == "normalized" and r["text_index"] == index
        ]
        persistent = [
            field
            for field in ("code", "date", "time")
            if sum(
                r.get("number_check", {}).get("fields", {}).get(field)
                in ("mismatch", "unparsed")
                for r in selected
            )
            >= 2
        ]
        bad = [
            r
            for r in selected
            if any(
                r.get("number_check", {}).get("fields", {}).get(field)
                in ("mismatch", "unparsed")
                for field in persistent
            )
        ]
        if bad:
            bad.sort(
                key=lambda r: (
                    "mismatch" not in r["number_check"]["fields"].values(),
                    r["seed"],
                )
            )
            result["persistent_normalized_issues"].append(
                {
                    "index": index,
                    "seed": bad[0]["seed"],
                    "fields": persistent,
                    "failing_seeds": [r["seed"] for r in bad],
                }
            )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    args.review.mkdir(parents=True, exist_ok=True)
    cards = []
    for issue in result["persistent_normalized_issues"]:
        index, seed = issue["index"], issue["seed"]
        pair = [r for r in rows if r["text_index"] == index and r["seed"] == seed]
        parts = [
            f"<section><h2>场景{index} · seed{seed}</h2><p>预期编号逐位：{html.escape('、'.join(manifest['cases'][index]['code']))}</p><p>核对编号有无漏位/错位，同时检查输入中的日期时间。转写只是筛查证据，不是已确认TTS错误。</p>"
        ]
        for row in sorted(pair, key=lambda r: r["profile"]):
            key = f"numbers-{row['profile']}-t{index}-s{seed}"
            wav = args.run / "audio" / (key + ".wav")
            transcript = transcripts.get(key, {}).get("heard", "缺失")
            inp = manifest[
                "normalized_texts" if row["profile"] == "normalized" else "texts"
            ][index]
            parts.append(
                f'<p><strong>{html.escape(row["profile"])}</strong> 输入：{html.escape(inp)}</p><audio controls preload="none" src="data:audio/wav;base64,{base64.b64encode(wav.read_bytes()).decode()}"></audio><details><summary>ASR与字段核对</summary><p>{html.escape(transcript)}</p><pre>{html.escape(json.dumps(row.get("number_check"), ensure_ascii=False))}</pre></details>'
            )
        cards.append("".join(parts) + "</section>")
    page = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>数字规范化残留疑点</title><style>body{max-width:900px;margin:30px auto;padding:15px;font:16px system-ui}section{padding:18px;border:1px solid #bbb;margin:20px 0}pre{white-space:pre-wrap}</style><h1>数字规范化残留疑点</h1><p>仅列规范化后同一字段至少两个seed仍未通过自动核对的场景，每场景一对。未人工听检，不需要比较细微音色。其他输出不要求回听。</p>'
        + (
            "".join(cards)
            or "<p>本轮未发现符合上述条件的残留数字问题，不要求人工回听。</p>"
        )
        + '<script>for(const a of document.querySelectorAll("audio"))a.addEventListener("play",()=>{for(const b of document.querySelectorAll("audio"))if(a!==b)b.pause();});</script></html>'
    )
    (args.review / "review.html").write_text(page)
    print(
        json.dumps(
            {"groups": result["groups"], "review_pairs": len(cards)},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
