#!/usr/bin/env python3
"""Build a local, self-contained paired listening pack; never rate quality."""

import argparse
import base64
import hashlib
import html
import json
import random
from pathlib import Path

LISTENING_FOCUS = {
    "t00-s1103": "数清两次“慢慢来”和两次“一步”，留意吞字、漏掉一次重复，以及重复是否生硬。末尾“地/的”是同音转写差异，不需要强行听出区别。",
    "t00-s2207": "先数清重复，再比较两次“慢慢来”之间的停顿和语速是否自然。差异很小就选“无明显差异”，不用猜哪一个更好。",
    "t00-s3301": "重点听“一步一步地做”是否连贯、完整，末字“做”有没有突然收断。“地/的”本身不是可据以判错的发音差异。",
    "t03-s2207": "听“想一想”之后是否真的多了一个“啊”，还是只是自然停顿；再确认“再试一试”完整。不要只按ASR文本判错。",
    "t11-s1103": "先核对日期2026年11月11日、时间15点30分，再逐位数编号：一、一、零、零、二、二。留意把编号读成数量、漏位或多位；不必纠结细小音色差别。",
    "t11-s2207": "重点数编号六位：一、一、零、零、二、二。开头应有两个“一”，中间应有两个“零”；日期和时间也需保持正确。",
    "t11-s3301": "对照两边的编号读法，预期逐位“一、一、零、零、二、二”，再确认“十五点三十分”。若两边都错，直接选“两者有问题”，不用选出赢家。",
    "t15-s1103": "先听整句，再听最后两秒，留意“放心好了”是否变成“放心好啦”，或末字缺失、收断。语气变化若不影响内容且难以区分，可选“无明显差异”；无法确定有无问题则选“无法判断”。",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(s) for s in (args.run / "trials.jsonl").read_text().splitlines()]
    manifest = json.loads((args.run / "manifest.json").read_text())
    transcripts = {
        r["key"]: r
        for r in [
            json.loads(s)
            for s in (args.run / "transcripts.jsonl").read_text().splitlines()
        ]
    }
    grouped = {}
    for row in rows:
        grouped.setdefault((row["text_index"], row["seed"]), {})[row["profile"]] = row
    rng = random.Random(20261023)
    cards, mapping = [], []
    for (index, seed), pair in sorted(grouped.items()):
        if set(pair) != {"baseline", "repetition_115"}:
            raise ValueError(f"Incomplete pair: {index}, {seed}")
        profiles = list(pair)
        rng.shuffle(profiles)
        flags = []
        for row in pair.values():
            if row.get("error") or row.get("stop_reason") != "eos":
                flags.append("结束异常")
            if not row.get("asr_exact"):
                flags.append("转写差异")
            if (row.get("pcm", {}).get("tail_ms") or 0) > 1000:
                flags.append("段尾超过1秒")
            if (row.get("raw", {}).get("leading_ms") or 0) > 1000:
                flags.append("原始前导超过1秒")
        flags = sorted(set(flags))
        pair_id = f"t{index:02d}-s{seed}"
        text = manifest["texts"][index]
        category = pair["baseline"]["category"]
        items, mapping_item = (
            [],
            {
                "id": pair_id,
                "category": category,
                "text": text,
                "seed": seed,
                "flags": flags,
                "mapping": {},
            },
        )
        for label, profile in zip(("A", "B"), profiles):
            row = pair[profile]
            key = f"validation-{profile}-t{index}-s{seed}"
            path = args.run / "audio" / f"{key}.wav"
            mapping_item["mapping"][label] = {
                "profile": profile,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()
                if path.exists()
                else None,
                "row": row,
                "asr": transcripts.get(key),
            }
            if path.exists():
                source = base64.b64encode(path.read_bytes()).decode()
                items.append(
                    f'<div>{label} <audio id="{pair_id}-{label}" preload="none" controls src="data:audio/wav;base64,{source}"></audio><button onclick="ending(\'{pair_id}-{label}\')">听最后2秒</button></div>'
                )
            else:
                items.append(f"<p>{label} 无音频，保留失败记录。</p>")
        selects = "".join(
            f'<label>{title}<select data-field="{field}"><option value="pending">待回听</option><option>均正常</option><option>无明显差异</option><option>A有问题</option><option>B有问题</option><option>两者有问题</option><option>无法判断</option></select></label>'
            for field, title in [
                ("words", "重复词/数字完整"),
                ("ending", "尾词与轻声"),
                ("prosody", "韵律与停顿"),
            ]
        )
        reveal = html.escape(
            json.dumps(
                {
                    label: {"profile": info["profile"], "asr": info["asr"]}
                    for label, info in mapping_item["mapping"].items()
                },
                ensure_ascii=False,
            )
        )
        focus = (
            f'<aside class="focus"><strong>本对听检重点</strong><p>{html.escape(LISTENING_FOCUS[pair_id])}</p></aside>'
            if pair_id in LISTENING_FOCUS
            else ""
        )
        card = f'<section data-id="{pair_id}"><h2>{pair_id} · {html.escape(category)} · {html.escape(" / ".join(flags) or "常规对照")}</h2><p>{html.escape(text)}</p>{focus}{"".join(items)}<div>{selects}</div><label>备注 <input data-field="notes" placeholder="例如：B漏了第二个谢谢；尾词听不清"></label><details><summary>评分后查看参数与ASR</summary><pre>{reveal}</pre></details></section>'
        cards.append((not bool(flags), index, seed, card))
        mapping.append(mapping_item)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "mapping.json").write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2)
    )
    template = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>1.15 配对回听</title><style>body{max-width:1000px;margin:30px auto;padding:0 18px;font:16px system-ui;background:#f6f7f9;color:#17202a}section{background:white;padding:20px;margin:16px 0;border:1px solid #ccd2da;border-radius:10px}h2{font-size:18px}.focus{background:#edf4ff;border-left:4px solid #3468aa;padding:10px 14px;margin:12px 0}.focus p{margin:6px 0}audio{vertical-align:middle;max-width:75%;margin:8px}label{display:inline-block;margin:8px}input{width:400px;max-width:65vw}pre{white-space:pre-wrap}button,select{padding:6px}header{position:sticky;top:0;background:#f6f7f9;padding:10px;border-bottom:1px solid #aaa}</style><header><strong>1.05 / 1.15 配对回听</strong> <button onclick="save()">导出评分JSON</button><span id="progress"></span></header><p>共 PAIRS 对，A/B逐对随机，异常/转写差异优先。尚未人工听检。先听完整句，再用末尾按钮定位；同一时刻只播放一个。文本有轻声词不等于读出了天然轻声，低音量不自动算漏读。数字书写与ASR差异需听音判断。</p><p>评分保存在当前浏览器本地，完成后请导出；换浏览器或文件路径可能无法恢复。音频内嵌，无需网络，不上传录音或评分。点击参数会揭示盲化映射。</p><p><strong>减少决策负担：</strong>只判断明确的漏字、错数字、异常结束。差别很小可选“无明显差异”，不必挑赢家；“无法判断”也可保留。自动检测只负责筛查，不会替你填写听感分。</p><p><strong>已记录你的反馈：</strong>长数字听感确实有问题，尚未指定哪对或A/B，也未归因为1.15。先核对编号逐位读法，暂不采用或部署候选。</p>CARDS<script>
const key='repetition-validation-20261020-v1';
function values(){return Array.from(document.querySelectorAll('section')).map(s=>({id:s.dataset.id,...Object.fromEntries(Array.from(s.querySelectorAll('[data-field]')).map(e=>[e.dataset.field,e.value]))}));}
function update(){localStorage.setItem(key,JSON.stringify(values()));document.querySelector('#progress').textContent=' 已评价 '+values().filter(r=>r.words!=='pending'||r.ending!=='pending'||r.prosody!=='pending').length+'/PAIRS';}
try{const old=JSON.parse(localStorage.getItem(key)||'[]');for(const row of old){const s=document.querySelector('section[data-id="'+row.id+'"]');if(s)for(const e of s.querySelectorAll('[data-field]'))if(row[e.dataset.field]!==undefined)e.value=row[e.dataset.field];}}catch(e){}
for(const e of document.querySelectorAll('[data-field]'))e.addEventListener('change',update);
for(const a of document.querySelectorAll('audio'))a.addEventListener('play',()=>{for(const b of document.querySelectorAll('audio'))if(a!==b)b.pause();});
async function ending(id){const a=document.getElementById(id);if(a.readyState<1){a.load();await new Promise(r=>a.addEventListener('loadedmetadata',r,{once:true}));}a.currentTime=Math.max(0,a.duration-2);await a.play();}
function save(){const blob=new Blob([JSON.stringify({version:1,exported:new Date().toISOString(),ratings:values()},null,2)],{type:'application/json'});const u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download='repetition-listening-ratings.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);}
update();</script></html>"""
    output = template.replace("PAIRS", str(len(cards))).replace(
        "CARDS", "\n".join(c[3] for c in sorted(cards))
    )
    (args.out / "review.html").write_text(output)
    print(
        json.dumps(
            {
                "pairs": len(cards),
                "priority_pairs": sum(not c[0] for c in cards),
                "audio_elements": output.count("<audio "),
                "html_bytes": len(output.encode()),
            }
        )
    )


if __name__ == "__main__":
    main()
