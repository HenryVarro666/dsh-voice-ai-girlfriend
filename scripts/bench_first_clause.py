#!/usr/bin/env python3
"""Replay fixed answer streams through three first-clause TTS policies."""

import argparse
import hashlib
import http.client
import json
import random
import time
import wave
from pathlib import Path

import numpy as np
from bench_tts_contention import gpu_snapshot, json_request

ANSWERS = [
    "先把重要文件备份好，再更新显卡驱动，最后重启电脑检查是否正常。这样出问题时也容易恢复。",
    "午饭可以先吃一碗面，再搭配鸡蛋和青菜，准备起来比较省时间。吃完以后再安排下午的事情。",
    "先检查服务日志里的报错，再确认端口和模型路径，最后重试一次请求。每次只改一个地方更容易定位问题。",
    "先把最紧急的任务做完，再处理需要集中精力的部分，最后留出时间检查结果。中途可以安排一次短休息。",
]
POLICIES = {"current_5": 5, "later_18": 18, "sentence": 100000}


def split(text, minimum):
    segments, buffer, first_done = [], "", False
    for ch in text:
        buffer += ch
        if ch in "。！？!?…" or (
            not first_done and ch in "，；：" and len(buffer) >= minimum
        ):
            segments.append(buffer)
            buffer, first_done = "", True
    if buffer:
        segments.append(buffer)
    assert "".join(segments) == text
    return segments


def capture(text, model):
    connection = http.client.HTTPConnection("127.0.0.1", 11434, timeout=60)
    connection.request(
        "POST",
        "/api/generate",
        json.dumps(
            {
                "model": model,
                "prompt": "只原样复制下面的文字，不添加任何说明、引号或换行：\n" + text,
                "stream": True,
                "think": False,
                "keep_alive": "10m",
                "options": {
                    "num_ctx": 32768,
                    "num_predict": 256,
                    "temperature": 0,
                    "seed": 42,
                },
            }
        ),
        {"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    if response.status != 200:
        raise RuntimeError(f"LLM capture returned {response.status}")
    output, packets, start = "", [], None
    for line in response:
        row = json.loads(line)
        if row.get("error"):
            raise RuntimeError(row["error"])
        if row.get("response"):
            now = time.perf_counter()
            start = start or now
            output += row["response"]
            packets.append(
                {"at_ms": round((now - start) * 1000, 3), "end_char": len(output)}
            )
        if row.get("done"):
            break
    connection.close()
    if output != text:
        raise RuntimeError(f"Capture was not exact: {output!r}")
    return packets


def synthesize(text, trial_start, cursor, output):
    connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=60)
    requested = time.perf_counter()
    connection.request(
        "POST",
        "/api/tts/stream",
        json.dumps({"text": text}),
        {"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    if response.status != 200:
        raise RuntimeError(f"TTS returned {response.status}")
    chunks, packets, pending = [], [], b""
    first_received = first_ideal = None
    under_ms = 0
    while True:
        chunk = response.read1(65536)
        at_ms = (time.perf_counter() - trial_start) * 1000
        if not chunk:
            break
        chunk = pending + chunk
        count = len(chunk) // 2 * 2
        pending = chunk[count:]
        if not count:
            continue
        chunk = chunk[:count]
        samples = np.frombuffer(chunk, dtype="<i2").astype(np.int32)
        above = np.flatnonzero(np.abs(samples) >= 328)
        if cursor is not None and at_ms > cursor:
            under_ms += at_ms - cursor
        played = max(cursor or at_ms, at_ms)
        if len(above) and first_received is None:
            first_received = at_ms
            first_ideal = played + int(above[0]) / 16
        cursor = played + len(samples) / 16
        packets.append(
            {
                "at_ms": round(at_ms, 3),
                "samples": len(samples),
                "first_voiced_index": int(above[0]) if len(above) else None,
            }
        )
        chunks.append(chunk)
    connection.close()
    if pending or first_received is None:
        raise RuntimeError("Invalid or silent TTS output")
    data = b"".join(chunks)
    output.extend(data)
    return {
        "text": text,
        "requested_ms": round((requested - trial_start) * 1000, 3),
        "first_voiced_received_ms": round(first_received, 3),
        "ideal_first_voice_ms": round(first_ideal, 3),
        "ideal_end_ms": round(cursor, 3),
        "underrun_ms": round(under_ms, 3),
        "audio_s": len(data) / 32000,
        "completed_ms": round((time.perf_counter() - trial_start) * 1000, 3),
        "packets": packets,
    }, cursor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if (args.out / "manifest.json").exists():
        parser.error("output directory already contains an experiment")
    initial = json_request("http://127.0.0.1:11434", "/api/ps")
    before = gpu_snapshot()
    if initial["models"] or before["free_mib"] < 8500 or before["utilization_pct"] > 20:
        raise RuntimeError(f"Expected idle GPU and empty Ollama: {before}")
    manifest = {
        "answers": ANSWERS,
        "policies": POLICIES,
        "repeats": args.repeats,
        "gpu_before": before,
        "initial_models": initial,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "Recorded copy-task text stream replay; HTTP PCM availability plus ideal playback estimate",
    }
    audio = args.out / "audio"
    audio.mkdir()
    model = "qwen3.5-voice:latest"
    try:
        json_request(
            "http://127.0.0.1:11434",
            "/api/generate",
            {
                "model": model,
                "prompt": "你好",
                "stream": False,
                "think": False,
                "keep_alive": "10m",
                "options": {"num_ctx": 32768, "num_predict": 1},
            },
        )
        manifest["captures"] = [capture(text, model) for text in ANSWERS]
        manifest["resident_models"] = json_request("http://127.0.0.1:11434", "/api/ps")
        (args.out / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2)
        )
        warm = bytearray()
        synthesize(ANSWERS[0], time.perf_counter(), None, warm)
        schedule = [
            (r, i, p)
            for r in range(args.repeats)
            for i in range(len(ANSWERS))
            for p in POLICIES
        ]
        random.Random(20261020).shuffle(schedule)
        for trial_id, (repeat, index, policy) in enumerate(schedule, 1):
            segments = split(ANSWERS[index], POLICIES[policy])
            row = {
                "trial_id": trial_id,
                "repeat": repeat,
                "text_index": index,
                "policy": policy,
                "segments": [],
            }
            started = time.perf_counter()
            end_char = 0
            cursor = None
            pcm = bytearray()
            for segment in segments:
                end_char += len(segment)
                ready = next(
                    p["at_ms"]
                    for p in manifest["captures"][index]
                    if p["end_char"] >= end_char
                )
                remaining = ready / 1000 - (time.perf_counter() - started)
                if remaining > 0:
                    time.sleep(remaining)
                result, cursor = synthesize(segment, started, cursor, pcm)
                result["text_ready_ms"] = ready
                row["segments"].append(result)
            row.update(
                first_text_ready_ms=row["segments"][0]["text_ready_ms"],
                first_voiced_received_ms=row["segments"][0]["first_voiced_received_ms"],
                ideal_first_voice_ms=row["segments"][0]["ideal_first_voice_ms"],
                ideal_end_ms=cursor,
                underrun_ms=sum(s["underrun_ms"] for s in row["segments"]),
                synthesis_ms=(time.perf_counter() - started) * 1000,
                audio_s=len(pcm) / 32000,
            )
            with wave.open(str(audio / f"trial-{trial_id:03d}.wav"), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(16000)
                out.writeframes(pcm)
            with (args.out / "trials.jsonl").open("a") as out:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                "CLAUSE",
                json.dumps({k: v for k, v in row.items() if k != "segments"}),
                flush=True,
            )
        manifest["completed"] = True
    finally:
        json_request(
            "http://127.0.0.1:11434", "/api/generate", {"model": model, "keep_alive": 0}
        )
        manifest["final_models"] = json_request("http://127.0.0.1:11434", "/api/ps")
        manifest["final_health"] = json_request("http://127.0.0.1:8765", "/api/health")
        (args.out / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2)
        )


if __name__ == "__main__":
    main()
