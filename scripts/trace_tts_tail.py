#!/usr/bin/env python3
"""Trace raw-model versus trimmed-handler TTS in a separate owned process."""

import argparse
import hashlib
import importlib.util
import json
import os
import random
import sys
import time
import wave
from pathlib import Path

import numpy as np
from bench_tts_contention import gpu_snapshot, json_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=40)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if (args.out / "manifest.json").exists():
        parser.error("output directory already contains an experiment")
    before = gpu_snapshot()
    if before["free_mib"] < 9000 or before["utilization_pct"] > 20:
        raise RuntimeError(f"GPU not ready: {before}")
    if json_request("http://127.0.0.1:11434", "/api/ps")["models"]:
        raise RuntimeError("Ollama must be empty; no existing models were unloaded")
    os.environ.setdefault("HF_HOME", str(Path.home() / "voice-ai/cache/hf"))
    os.environ["HF_HUB_OFFLINE"] = "1"
    import torch
    from speech_to_speech.pipeline.messages import TTSInput

    torch.set_num_threads(4)
    bridge_file = args.repo / "bridge/voice_bridge.py"
    sys.path.insert(0, str(bridge_file.parent))
    spec = importlib.util.spec_from_file_location("isolated_trace_bridge", bridge_file)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    manifest = {
        "scope": "Isolated model/handler; no HTTP queue or browser timing",
        "bridge_sha256": hashlib.sha256(bridge_file.read_bytes()).hexdigest(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "gpu_before": before,
        "voice": bridge._current_persona,
        "text": "好呀，我们开始吧。",
        "runs": args.runs,
        "warmup_excluded": True,
    }
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)
    )
    loaded_at = time.perf_counter()
    handler = bridge._load_tts_handler()
    manifest["load_s"] = round(time.perf_counter() - loaded_at, 2)
    manifest["gpu_loaded"] = gpu_snapshot()
    if manifest["gpu_loaded"]["free_mib"] < 1500:
        raise RuntimeError("Insufficient remaining VRAM after isolated load")
    for _ in range(2):
        list(handler.process(TTSInput(text=manifest["text"], language_code="zh")))
    print("ISOLATED_MODEL_READY", json.dumps(manifest["gpu_loaded"]), flush=True)
    original = handler.model.generate_voice_clone_streaming
    trace = {}

    def traced(*positional, **keywords):
        samples_seen = 0
        for item in original(*positional, **keywords):
            elapsed = (time.perf_counter() - trace["started"]) * 1000
            chunk, sr = handler._prepare_audio_chunk(item)
            if chunk is not None and sr is not None and chunk.size:
                values = np.asarray(chunk).reshape(-1)
                voiced = np.flatnonzero(np.abs(values) >= 0.01)
                event = {
                    "at_ms": round(elapsed, 2),
                    "samples": len(values),
                    "sr": sr,
                    "peak": round(float(np.abs(values).max()), 6),
                    "voiced": bool(len(voiced)),
                }
                trace["raw_events"].append(event)
                trace.setdefault("first_raw_ms", elapsed)
                if len(voiced) and "first_raw_voiced_ms" not in trace:
                    trace["first_raw_voiced_ms"] = elapsed
                    trace["raw_leading_silence_ms"] = (
                        (samples_seen + int(voiced[0])) / sr * 1000
                    )
                samples_seen += len(values)
            yield item

    handler.model.generate_voice_clone_streaming = traced
    (args.out / "audio").mkdir(exist_ok=True)

    def run(seed, phase):
        nonlocal trace
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        trace = {"started": time.perf_counter(), "raw_events": []}
        first_handler = None
        chunks = []
        for chunk in handler.process(
            TTSInput(text=manifest["text"], language_code="zh")
        ):
            first_handler = (
                first_handler or (time.perf_counter() - trace["started"]) * 1000
            )
            chunks.append(
                np.frombuffer(chunk, dtype="<i2")
                if isinstance(chunk, bytes)
                else np.asarray(chunk, dtype=np.int16)
            )
        if not chunks:
            raise RuntimeError("Handler produced no PCM")
        samples = np.concatenate(chunks)
        row = {k: v for k, v in trace.items() if k != "started"}
        row.update(
            seed=seed,
            phase=phase,
            first_handler_ms=round(first_handler, 2),
            total_ms=round((time.perf_counter() - trace["started"]) * 1000, 2),
            audio_s=len(samples) / 16000,
        )
        filename = f"{phase}-{seed:03d}.wav"
        with wave.open(str(args.out / "audio" / filename), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(samples.astype("<i2").tobytes())
        with (args.out / "trials.jsonl").open("a") as output:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            "TRACE",
            json.dumps({k: v for k, v in row.items() if k != "raw_events"}),
            flush=True,
        )
        return row

    rows = []
    try:
        for seed in range(args.runs):
            rows.append(run(seed, "main"))
        slow = [r["seed"] for r in rows if r["first_handler_ms"] > 1000][:3]
        for seed in slow:
            run(seed, "replay")
        manifest["completed"] = True
        manifest["slow_seeds_replayed"] = slow
    finally:
        handler.model.generate_voice_clone_streaming = original
        handler.cleanup()
        (args.out / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2)
        )


if __name__ == "__main__":
    main()
