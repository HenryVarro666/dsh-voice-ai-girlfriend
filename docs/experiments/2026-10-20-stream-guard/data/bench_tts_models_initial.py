#!/usr/bin/env python3
"""Isolated same-engine Base-model comparison; never edits production config."""

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
from bench_tts_contention import TEXTS, gpu_snapshot, json_request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from stream_tail import TailGuard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if (args.out / "manifest.json").exists():
        parser.error("output already contains a run")
    before = gpu_snapshot()
    if before["free_mib"] < 9000 or before["utilization_pct"] > 20:
        raise RuntimeError(f"GPU not ready: {before}")
    if json_request("http://127.0.0.1:11434", "/api/ps")["models"]:
        raise RuntimeError("Ollama is not empty; will not unload others' models")
    os.environ.setdefault("HF_HOME", str(Path.home() / "voice-ai/cache/hf"))
    os.environ["HF_HUB_OFFLINE"] = "1"
    import torch
    from speech_to_speech.pipeline.messages import TTSInput

    torch.set_num_threads(4)
    module_path = args.repo / "bridge/voice_bridge.py"
    sys.path.insert(0, str(module_path.parent))
    spec = importlib.util.spec_from_file_location("owned_model_comparison", module_path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    bridge.CONFIG["tts"]["model_name"] = args.model
    manifest = {
        "label": args.label,
        "model": args.model,
        "seeds": [0, 16, 25],
        "texts": TEXTS,
        "scope": "Isolated handler and streaming guard, no HTTP/LLM/browser",
        "gpu_before": before,
        "bridge_sha256": hashlib.sha256(module_path.read_bytes()).hexdigest(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "guard_sha256": hashlib.sha256(
            Path(__file__)
            .resolve()
            .parents[1]
            .joinpath("bridge/stream_tail.py")
            .read_bytes()
        ).hexdigest(),
    }
    manifest_path = args.out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    started = time.perf_counter()
    handler = bridge._load_tts_handler()
    try:
        manifest["load_ms"] = (time.perf_counter() - started) * 1000
        manifest["gpu_loaded"] = gpu_snapshot()
        manifest["handler"] = {
            key: getattr(handler, key)
            for key in [
                "streaming_chunk_size",
                "non_streaming_mode",
                "xvec_only",
                "parity_mode",
            ]
        }
        manifest["reference_sha256"] = hashlib.sha256(
            Path(handler.ref_audio).read_bytes()
        ).hexdigest()
        if manifest["gpu_loaded"]["free_mib"] < 1500:
            raise RuntimeError("Less than 1500 MiB free after owned model load")
        for _ in range(2):
            list(handler.process(TTSInput(text=TEXTS[0], language_code="zh")))
        print("READY", args.label, json.dumps(manifest["gpu_loaded"]), flush=True)
        (args.out / "audio").mkdir(exist_ok=True)
        rows = [(t, seed) for t in range(len(TEXTS)) for seed in manifest["seeds"]]
        random.Random(20261020).shuffle(rows)
        for text_index, seed in rows:
            random.seed(seed)
            np.random.seed(seed)
            torch.manual_seed(seed)
            t0 = time.perf_counter()
            packets, original_parts, guarded_parts, guarded_packets = [], [], [], []
            guard = TailGuard()

            def source():
                for chunk in handler.process(
                    TTSInput(text=TEXTS[text_index], language_code="zh")
                ):
                    data = (
                        chunk
                        if isinstance(chunk, bytes)
                        else np.asarray(chunk, dtype="<i2").tobytes()
                    )
                    if not data:
                        continue
                    packets.append(
                        {
                            "at_ms": (time.perf_counter() - t0) * 1000,
                            "samples": len(data) // 2,
                        }
                    )
                    original_parts.append(data)
                    yield data

            for part in guard.process(source()):
                guarded_packets.append(
                    {
                        "at_ms": (time.perf_counter() - t0) * 1000,
                        "samples": len(part) // 2,
                    }
                )
                guarded_parts.append(part)
            total_ms = (time.perf_counter() - t0) * 1000
            key = f"text-{text_index}-seed-{seed:02d}"
            row = {
                "label": args.label,
                "text_index": text_index,
                "seed": seed,
                "total_ms": total_ms,
                "gpu_after": gpu_snapshot(),
                "peak_pending_bytes": guard.peak_pending_bytes,
                "overflows": guard.overflows,
                "removed_ms": guard.removed_samples / 16,
            }
            for kind, parts, events in [
                ("original", original_parts, packets),
                ("guarded", guarded_parts, guarded_packets),
            ]:
                raw = b"".join(parts)
                samples = np.frombuffer(raw, dtype="<i2").astype(np.int32)
                active = np.flatnonzero(np.abs(samples) >= 99)
                voice = np.flatnonzero(np.abs(samples) >= 328)
                seen = 0
                first_voiced_ms = None
                for event in events:
                    part = samples[seen : seen + event["samples"]]
                    if first_voiced_ms is None and np.any(np.abs(part) >= 328):
                        first_voiced_ms = event["at_ms"]
                    seen += event["samples"]
                assert seen == len(samples)
                with wave.open(
                    str(args.out / "audio" / f"{key}-{kind}.wav"), "wb"
                ) as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(16000)
                    wav.writeframes(raw)
                row[kind] = {
                    "audio_ms": len(samples) / 16,
                    "first_pcm_ms": events[0]["at_ms"] if events else None,
                    "first_voiced_ms": first_voiced_ms,
                    "leading_ms_1pct": int(voice[0]) / 16 if len(voice) else None,
                    "tail_ms": (len(samples) - int(active[-1]) - 1) / 16
                    if len(active)
                    else None,
                    "packets": events,
                }
            with (args.out / "trials.jsonl").open("a") as out:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                "TRIAL",
                args.label,
                text_index,
                seed,
                round(row["original"]["first_voiced_ms"] or 0, 1),
                round(total_ms, 1),
                round(row["removed_ms"], 1),
                flush=True,
            )
        manifest["completed"] = True
    finally:
        handler.cleanup()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
