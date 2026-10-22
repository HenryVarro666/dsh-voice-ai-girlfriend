#!/usr/bin/env python3
"""Build a private, byte-preserving and initially unrated A/B listening page."""

import argparse
import base64
import hashlib
import html
import io
import json
import random
import wave
from pathlib import Path

import numpy as np

TEMPLATE = Path(__file__).resolve().parents[1] / "docs/examples/xvector-review.html"
FOCUS = [
    "听句末“吧”是否完整，语气是否自然落下；再和参考比较声音的辨识度。",
    "听“陪着你呢”的连贯性和收尾，不要把轻声或音量较小自动当作漏字。",
    "听问句的节奏与句末“吗”，留意问句是否生硬抬高、尾字是否突然收断。",
    "听“处理好了”的“了”有没有读出，以及前后停顿是否自然。",
    "听“盒子”“椅子”的“子”，留意词尾是否完整、轻重关系是否自然。",
    "听“谢谢”和“好多了”的节奏、情绪与尾字；不用强行选择细微差异。",
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inspect_audio(data):
    with wave.open(io.BytesIO(data)) as wav:
        if (
            wav.getnchannels() != 1
            or wav.getsampwidth() != 2
            or wav.getframerate() != 16000
        ):
            raise ValueError("Expected the original mono 16kHz PCM16 handler WAV")
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(
            np.int32
        )
    if not samples.size:
        raise ValueError("Empty audio must not disappear from a listening pair")
    active = np.flatnonzero(np.abs(samples) >= 99)
    return {
        "sha256": digest(data),
        "samples": len(samples),
        "duration_s": len(samples) / 16000,
        # Seek only. Playback continues through the untouched low-amplitude tail.
        "ending_start_s": max(0, int(active[-1]) / 16000 - 2) if active.size else 0,
    }


def collect(run, dataset):
    manifest = json.loads((run / "manifest.json").read_text())
    rows = [
        json.loads(line) for line in (run / "trials.jsonl").read_text().splitlines()
    ]
    pairs = {}
    for row in rows:
        if row["profile"] not in ("baseline", "xvector"):
            continue
        key = (row["phase"], row["text_index"], row["seed"])
        pair = pairs.setdefault(key, {})
        if row["profile"] in pair:
            raise ValueError(f"Duplicate output: {dataset} {key}")
        pair[row["profile"]] = row
    result = []
    for (phase, index, seed), rows_by_mode in sorted(pairs.items()):
        if set(rows_by_mode) != {"baseline", "xvector"}:
            raise ValueError(f"Incomplete pair: {dataset} {phase} {index} {seed}")
        text = manifest[phase]["texts"][index]
        priority = (dataset == "endings" and seed == 16) or (
            dataset == "layout"
            and phase == "holdout"
            and seed == 4201
            and index in (1, 5)
        )
        focus = (
            FOCUS[index]
            if dataset == "endings"
            else (
                "听重复的“谢谢”是否完整、停顿是否自然，再比较音色。"
                if phase == "holdout" and index == 1
                else "先听完整句，比较长句的分组、语速与句尾收束，再听是否接近参考音色。"
                if phase == "holdout" and index == 5
                else "比较音色是否接近参考、停顿是否自然，以及末字是否完整；长静音本身不等于尾词丢失。"
            )
        )
        audio = {}
        for mode, row in rows_by_mode.items():
            if row.get("error"):
                raise ValueError(
                    f"Keep failed output visible before review: {row['key']}"
                )
            data = (run / "audio" / (row["key"] + ".wav")).read_bytes()
            audio[mode] = {"data": data, **inspect_audio(data)}
        result.append(
            {
                "source": dataset,
                "phase": phase,
                "index": index,
                "seed": seed,
                "text": text,
                "focus": focus,
                "priority": priority,
                "audio": audio,
            }
        )
    return result, manifest["reference_sha256"]


def select(field, title, options):
    values = '<option value="pending">待试听</option>' + "".join(
        f'<option value="{value}">{label}</option>' for value, label in options
    )
    return f'<label>{title}<select data-field="{field}" aria-label="{title}">{values}</select></label>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout", required=True, type=Path)
    parser.add_argument("--endings", required=True, type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    pairs, ref_hash = collect(args.layout, "layout")
    added, added_hash = collect(args.endings, "endings")
    reference = args.reference.read_bytes()
    if ref_hash != digest(reference) or added_hash != ref_hash:
        raise ValueError("Reference differs across generation runs or listening anchor")
    pairs += added
    if len(pairs) != 32 or sum(p["priority"] for p in pairs) != 8:
        raise ValueError(
            "Expected all 20 existing plus 12 new pairs and 8 priority pairs"
        )
    pairs.sort(
        key=lambda p: (
            not p["priority"],
            p["source"],
            p["phase"],
            p["index"],
            p["seed"],
        )
    )
    rng = random.Random(20261024)
    labels = [False] * 16 + [True] * 16
    rng.shuffle(labels)
    cards, mapping = [], []
    for index, (pair, swap) in enumerate(zip(pairs, labels), 1):
        pair_id = f"p{index:02d}"
        modes = ("xvector", "baseline") if swap else ("baseline", "xvector")
        info = {k: v for k, v in pair.items() if k != "audio"}
        info.update(id=pair_id, mapping={})
        players = []
        for label, mode in zip(("A", "B"), modes):
            audio = pair["audio"][mode]
            info["mapping"][label] = {k: v for k, v in audio.items() if k != "data"} | {
                "mode": mode
            }
            encoded = base64.b64encode(audio["data"]).decode()
            players.append(f'''<div class="player"><strong>{label}</strong>
<audio id="{pair_id}-{label}" controls preload="metadata" aria-label="{pair_id} 音频{label}" src="data:audio/wav;base64,{encoded}"></audio>
<button data-play="{pair_id}-{label}">播放 {label}</button>
<button data-ending="{pair_id}-{label}" data-seek="{audio["ending_start_s"]:.6f}">听 {label} 尾词</button></div>''')
        scores = select(
            "timbre",
            "音色：谁更接近上方参考？",
            [
                ("A", "A更接近"),
                ("B", "B更接近"),
                ("same", "无明显差异"),
                ("both_bad", "两者都差得明显"),
                ("unsure", "无法判断"),
            ],
        )
        scores += select(
            "prosody",
            "韵律：语速、停顿与语气",
            [
                ("A", "A更自然"),
                ("B", "B更自然"),
                ("same", "无明显差异"),
                ("both_bad", "两者都有问题"),
                ("unsure", "无法判断"),
            ],
        )
        scores += select(
            "ending",
            "结尾：末字与轻声收尾",
            [
                ("both_ok", "两者都完整自然"),
                ("A_bad", "A有缺字或生硬收断"),
                ("B_bad", "B有缺字或生硬收断"),
                ("A", "A更自然，但都完整"),
                ("B", "B更自然，但都完整"),
                ("both_bad", "两者都有问题"),
                ("unsure", "无法判断"),
            ],
        )
        cards.append(f'''<section class="pair" data-id="{pair_id}" data-priority="{str(pair["priority"]).lower()}">
<div class="pair-heading"><span>{pair_id} · {"优先试听" if pair["priority"] else "补充对照"}</span><button data-sequence="{pair_id}">对比 A → B</button></div>
<h2>{html.escape(pair["text"])}</h2><p class="focus">{html.escape(pair["focus"])}</p>
<div class="players">{"".join(players)}</div><div class="scores">{scores}</div>
<label class="note">备注<input data-field="notes" aria-label="{pair_id} 备注" placeholder="例如：B的呢收得太突然；两边都像参考"></label>
<p class="revealed" hidden></p></section>''')
        mapping.append(info)
    pack_hash = digest(json.dumps(mapping, sort_keys=True, ensure_ascii=False).encode())
    pack_id = "xvector-listening-20261020-" + pack_hash[:12]
    reveal = [
        {
            "id": p["id"],
            "mapping": {label: info["mode"] for label, info in p["mapping"].items()},
        }
        for p in mapping
    ]
    output = TEMPLATE.read_text().replace("@@CARDS@@", "\n".join(cards))
    output = output.replace("@@REFERENCE@@", base64.b64encode(reference).decode())
    output = output.replace("@@PACK_ID@@", pack_id).replace("@@PACK_HASH@@", pack_hash)
    output = output.replace(
        "@@REVEAL@@", base64.b64encode(json.dumps(reveal).encode()).decode()
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "review.html").write_text(output)
    (args.out / "mapping.json").write_text(
        json.dumps(
            {
                "pack_id": pack_id,
                "mapping_sha256": pack_hash,
                "reference_sha256": ref_hash,
                "pairs": mapping,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    (args.out / "unrated.json").write_text(
        json.dumps(
            {
                "pack_id": pack_id,
                "ratings": [
                    {
                        "id": p["id"],
                        "timbre": "pending",
                        "prosody": "pending",
                        "ending": "pending",
                        "notes": "",
                    }
                    for p in mapping
                ],
            },
            indent=2,
        )
    )
    print(
        json.dumps(
            {
                "pack_id": pack_id,
                "pairs": len(pairs),
                "audio_elements": output.count("<audio "),
                "bytes": len(output.encode()),
                "baseline_on_A": sum(not s for s in labels),
                "human_ratings": "pending",
            }
        )
    )


if __name__ == "__main__":
    main()
