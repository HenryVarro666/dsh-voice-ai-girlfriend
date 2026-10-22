#!/usr/bin/env python3
"""Create a self-contained listening sheet and separate automated ASR checks."""

import argparse
import base64
import html
import json
import urllib.request
import wave
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder
    manifest = json.loads((folder / "fixtures.json").read_text())
    if (folder / "listening-asr.json").exists():
        parser.error("checks already exist")
    results, rows = [], []
    for index in range(6):
        item = next(f for f in manifest["fixtures"] if f["id"] == f"soft-{index}")
        cells = []
        for policy in ["baseline", "guard99", "guard3"]:
            for kind, name in [
                ("full", f"soft-{index}-{policy}.wav"),
                ("amplified_suffix", f"review-{index}-{policy}.wav"),
            ]:
                raw = (folder / "audio" / name).read_bytes()
                request = urllib.request.Request(
                    "http://127.0.0.1:8765/api/stt",
                    data=raw,
                    headers={"Content-Type": "audio/wav"},
                )
                with urllib.request.urlopen(request, timeout=90) as response:
                    asr = json.load(response)
                results.append(
                    {
                        "id": item["id"],
                        "policy": policy,
                        "kind": kind,
                        "recognized": asr["text"],
                    }
                )
            # Only the suffix is boosted, so all three versions are audible.
            raw = (folder / "audio" / f"review-{index}-{policy}.wav").read_bytes()
            encoded = base64.b64encode(raw).decode()
            with wave.open(
                str(folder / "audio" / f"review-{index}-{policy}.wav")
            ) as source:
                empty = source.getnframes() == 0
            player = (
                "<p>尾部已全部裁掉，无音频可播</p>"
                if empty
                else f'<audio controls preload="metadata" src="data:audio/wav;base64,{encoded}"></audio>'
            )
            cells.append(
                f'<td>{player}<br><select data-id="{item["id"]}" data-policy="{policy}"><option value="pending">未听</option><option value="complete">尾词完整</option><option value="cut">有少字</option><option value="uncertain">不确定</option></select></td>'
            )
        rows.append(
            f"<tr><td>{index + 1}. {html.escape(item['text'])}</td>{''.join(cells)}</tr>"
        )
    document = (
        """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>轻声结尾回听对照</title><style>body{font:16px system-ui;max-width:1240px;margin:36px auto;padding:0 20px;color:#172d3d;background:#f5f8fa}table{width:100%;border-collapse:collapse;background:white}th,td{padding:14px;border:1px solid #cbd5df;vertical-align:top}audio{width:240px}select,button{padding:8px}p{line-height:1.7}.notice{background:#fff1cc;padding:15px;border-radius:8px}</style><h1>轻声结尾回听对照</h1><p>2026-10-20 · 合成语音与受控降幅；尾部峰值70，后接1.2秒数字静音。以下仅放大尾部，便于听清是否少字。三列独立归一化，不用于比较自然响度或音质。</p><p class="notice">人工听检状态：待评分。自动转写与波形检查不能替代回听。阈值3在更弱语音上也可能误裁，尚未上线。</p><table><thead><tr><th>原句（播放器中仅听末尾部分）</th><th>原始轻声尾部</th><th>阈值99候选</th><th>阈值3候选</th></tr></thead><tbody>"""
        + "".join(rows)
        + """</tbody></table><p>请戴耳机，先听原始尾部，再比较两列候选。若原始也听不清，选“不确定”。评分只留在此页面和下载文件，不向服务器发送。</p><button id="export">下载听检评分 JSON</button><script>document.querySelector('#export').onclick=()=>{const rows=[...document.querySelectorAll('select')].map(s=>({id:s.dataset.id,policy:s.dataset.policy,rating:s.value}));const blob=new Blob([JSON.stringify({reviewer:'user',created_at:new Date().toISOString(),rows},null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='listening-ratings.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}</script></html>"""
    )
    responsive = """@media(max-width:900px){table,tbody,tr,td{display:block}thead{display:none}tr{margin-bottom:18px;border:1px solid #cbd5df}td{border:0}audio{width:100%}td:nth-child(2):before{content:'原始轻声尾部';display:block}td:nth-child(3):before{content:'阈值99候选';display:block}td:nth-child(4):before{content:'阈值3候选';display:block}}"""
    document = document.replace("</style>", responsive + "</style>")
    (folder / "review.html").write_text(document)
    (folder / "listening-asr.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2)
    )
    (folder / "human-review.json").write_text(
        json.dumps({"status": "pending", "ratings": []}, indent=2)
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
