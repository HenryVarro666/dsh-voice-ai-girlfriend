#!/usr/bin/env python3
"""Diagnose EOS and sampling in an owned model process; never edit services."""

import argparse
import hashlib
import importlib.util
import json
import os
import random
import re
import sys
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np
from bench_tts_contention import TEXTS, gpu_snapshot, json_request
from generation_end_metrics import audio_stats, choose_candidate, classify_stop

PROFILES = {
    "baseline": {},
    "temperature_05": {"temperature": 0.5},
    "repetition_115": {"repetition_penalty": 1.15},
    "eos_bias_1": {"eos_bias": 1.0},
}
HOLDOUT = [
    "今天先休息吧，明天再继续。",
    "你说的是星期四上午九点吗？",
    "我想确认一下，最后一个字是轻声的呢。",
    "请先保存文件，等检查完成以后，再关闭窗口。",
]


def normalize(text):
    return re.sub(r"[\W_]", "", text).lower()


def edit_distance(a, b):
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


def asr(audio):
    request = urllib.request.Request(
        "http://127.0.0.1:8765/api/stt",
        data=audio,
        headers={"Content-Type": "application/octet-stream"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)["text"]


def write_wav(path, audio, sr):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sr)
        output.writeframes(audio)


def summary(rows):
    return {
        "eos": all(r.get("stop_reason") == "eos" and r.get("asr_ok") for r in rows),
        "edits": sum(r.get("edit_distance", 9999) for r in rows),
        "long_tails": sum((r.get("pcm", {}).get("tail_ms") or 0) > 1000 for r in rows),
        "tail_ms": sum(r.get("pcm", {}).get("tail_ms") or 0 for r in rows),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=0)
    args = parser.parse_args()
    deadline = time.monotonic() + args.wait_seconds
    while True:
        before = gpu_snapshot()
        resident = json_request("http://127.0.0.1:11434", "/api/ps")["models"]
        if (
            before["free_mib"] >= 9000
            and before["utilization_pct"] <= 20
            and not resident
        ):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"GPU not ready; no unload attempted: {before}")
        print("WAITING_FOR_OWN_RESOURCE_WINDOW", json.dumps(before), flush=True)
        time.sleep(10)
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "audio").mkdir()
    os.environ.setdefault("HF_HOME", str(Path.home() / "voice-ai/cache/hf"))
    os.environ["HF_HUB_OFFLINE"] = "1"
    import faster_qwen3_tts.streaming as streaming
    import torch
    from speech_to_speech.pipeline.messages import TTSInput

    torch.set_num_threads(4)
    module_path = args.repo / "bridge/voice_bridge.py"
    sys.path.insert(0, str(module_path.parent))
    spec = importlib.util.spec_from_file_location("owned_eos_bridge", module_path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    manifest = {
        "scope": "isolated model/handler; no PCM cropping; all EOS hooks owned",
        "gpu_before": before,
        "profiles": PROFILES,
        "texts": TEXTS,
        "diagnostic_seeds": [0, 16, 25],
        "holdout_texts": HOLDOUT,
        "holdout_seeds": [606, 707, 808],
        "hashes": {},
    }
    for path in (Path(__file__), Path(streaming.__file__), module_path):
        manifest["hashes"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    tload = time.perf_counter()
    handler = bridge._load_tts_handler()
    manifest["load_ms"] = (time.perf_counter() - tload) * 1000
    manifest["gpu_loaded"] = gpu_snapshot()
    manifest["reference_sha256"] = hashlib.sha256(
        Path(handler.ref_audio).read_bytes()
    ).hexdigest()
    manifest["handler"] = {
        key: getattr(handler, key)
        for key in (
            "streaming_chunk_size",
            "non_streaming_mode",
            "xvec_only",
            "parity_mode",
            "max_new_tokens",
        )
    }
    manifest_path = args.out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    original_model = handler.model.generate_voice_clone_streaming
    original_stream = streaming.fast_generate_streaming
    original_sample = streaming.sample_logits
    trace = {}

    def sample(logits, **kwargs):
        if not trace["instrumented"] and not trace["profile"].get("eos_bias"):
            return original_sample(logits, **kwargs)
        eos = trace["eos_id"]
        # Keep GPU copies, transfer/score after generation to avoid per-step CPU sync.
        scores = logits.detach().clone()
        if trace["profile"].get("eos_bias"):
            logits = logits.clone()
            logits[..., eos] += trace["profile"]["eos_bias"]
        token = original_sample(logits, **kwargs)
        if trace["instrumented"]:
            trace["sampling"].append((scores, token.detach().clone(), kwargs.copy()))
        return token

    def codec_stream(**kwargs):
        trace["eos_id"] = kwargs["config"].codec_eos_token_id
        trace["cap"] = kwargs["max_new_tokens"]
        trace["effective"] = {
            key: kwargs[key]
            for key in (
                "temperature",
                "top_k",
                "top_p",
                "repetition_penalty",
                "do_sample",
                "min_new_tokens",
            )
        }
        for codes, timing in original_stream(**kwargs):
            trace["frames"] += codes.shape[0]
            yield codes, timing

    def model_stream(*args, **kwargs):
        changes = {k: v for k, v in trace["profile"].items() if k != "eos_bias"}
        kwargs.update(changes)
        for item in original_model(*args, **kwargs):
            chunk, sr = handler._prepare_audio_chunk(item)
            if chunk is not None and sr is not None and chunk.size:
                trace["raw"].append(np.asarray(chunk).reshape(-1).copy())
                trace["raw_sr"] = sr
            yield item

    def run(profile, text_index, seed, phase, instrumented=True):
        nonlocal trace
        text = (HOLDOUT if phase == "holdout" else TEXTS)[text_index]
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        trace = {
            "profile": PROFILES[profile],
            "instrumented": instrumented,
            "sampling": [],
            "raw": [],
            "frames": 0,
        }
        t0 = time.perf_counter()
        parts, first_pcm, first_voiced = [], None, None
        row = {
            "profile": profile,
            "text_index": text_index,
            "seed": seed,
            "phase": phase,
        }
        try:
            for chunk in handler.process(TTSInput(text=text, language_code="zh")):
                data = (
                    chunk
                    if isinstance(chunk, bytes)
                    else np.asarray(chunk, dtype="<i2").tobytes()
                )
                if not data:
                    continue
                now = (time.perf_counter() - t0) * 1000
                first_pcm = first_pcm if first_pcm is not None else now
                if first_voiced is None and np.any(
                    np.abs(np.frombuffer(data, dtype="<i2").astype(np.int32)) >= 328
                ):
                    first_voiced = now
                parts.append(data)
            torch.cuda.synchronize()
            row["generation_ms"] = (time.perf_counter() - t0) * 1000
            pcm = b"".join(parts)
            values = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            row.update(
                first_pcm_ms=first_pcm,
                first_voiced_ms=first_voiced,
                pcm=audio_stats(values, 16000, 99 / 32768),
                frames=trace["frames"],
                cap=trace["cap"],
                effective=trace["effective"],
            )
            key = f"{phase}-{profile}-t{text_index}-s{seed}"
            write_wav(args.out / "audio" / f"{key}.wav", pcm, 16000)
            raw = np.concatenate(trace["raw"]) if trace["raw"] else np.array([])
            row["raw"] = audio_stats(raw, trace.get("raw_sr", 24000), 0.003)
            row["raw_sr"] = trace.get("raw_sr")
            sampled, steps = [], []
            for scores, token, kw in trace["sampling"]:
                scores = scores.flatten().float().cpu()
                eos = trace["eos_id"]
                if trace["profile"].get("eos_bias"):
                    scores[eos] += trace["profile"]["eos_bias"]
                mask = kw.get("suppress_mask")
                if mask is not None:
                    scores[mask.cpu()] = -float("inf")
                if kw.get("suppress_tokens"):
                    scores[list(kw["suppress_tokens"])] = -float("inf")
                rank = int((scores > scores[eos]).sum().item()) + 1
                scores = scores / kw["temperature"]
                if kw["top_k"] > 0:
                    threshold = torch.topk(
                        scores, min(kw["top_k"], len(scores))
                    ).values[-1]
                    scores[scores < threshold] = -float("inf")
                picked = int(token.item())
                sampled.append(picked)
                steps.append(
                    {
                        "step": len(steps),
                        "eos_probability": float(torch.softmax(scores, dim=-1)[eos]),
                        "eos_rank": rank,
                        "eos_sampled": picked == eos,
                    }
                )
            row["stop_reason"] = (
                classify_stop(sampled, trace["eos_id"], trace["frames"], trace["cap"])
                if instrumented
                else "untraced"
            )
            row["eos_steps"] = steps
            row["sampling_steps"] = len(sampled)
            transcript = asr(pcm)
            row["asr_ok"] = True
            row["edit_distance"] = edit_distance(normalize(transcript), normalize(text))
            row["asr_exact"] = normalize(transcript) == normalize(text)
            row["asr_ending_match"] = normalize(transcript).endswith(
                normalize(text)[-4:]
            )
            # Human-readable transcript stays only on the GPU host.
            with (args.out / "transcripts.jsonl").open("a") as f:
                f.write(
                    json.dumps(
                        {"key": key, "expected": text, "heard": transcript},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception as exc:
            row.update(error=type(exc).__name__ + ": " + str(exc), asr_ok=False)
        with (args.out / "trials.jsonl").open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            "TRIAL",
            phase,
            profile,
            text_index,
            seed,
            row.get("stop_reason"),
            round(row.get("pcm", {}).get("tail_ms") or 0),
            row.get("edit_distance"),
            flush=True,
        )
        return row

    try:
        if manifest["gpu_loaded"]["free_mib"] < 1500:
            raise RuntimeError("Insufficient remaining VRAM")
        for _ in range(2):
            list(handler.process(TTSInput(text=TEXTS[0], language_code="zh")))
        streaming.fast_generate_streaming = codec_stream
        streaming.sample_logits = sample
        handler.model.generate_voice_clone_streaming = model_stream
        for seed in (16, 25):
            run("baseline", 0, seed, "control", False)
        grid = [(p, t, s) for p in PROFILES for t in range(4) for s in (0, 16, 25)]
        random.Random(20261020).shuffle(grid)
        rows = [run(p, t, s, "diagnostic") for p, t, s in grid]
        summaries = {
            p: summary([r for r in rows if r["profile"] == p]) for p in PROFILES
        }
        chosen = choose_candidate(
            summaries["baseline"],
            {p: r for p, r in summaries.items() if p != "baseline"},
        )
        manifest.update(diagnostic_summary=summaries, chosen=chosen)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        if chosen:
            grid = [
                (p, t, s)
                for p in ("baseline", chosen)
                for t in range(4)
                for s in (606, 707, 808)
            ]
            random.Random(20261021).shuffle(grid)
            for p, t, s in grid:
                run(p, t, s, "holdout")
        manifest["completed"] = True
    finally:
        handler.model.generate_voice_clone_streaming = original_model
        streaming.fast_generate_streaming = original_stream
        streaming.sample_logits = original_sample
        handler.cleanup()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
