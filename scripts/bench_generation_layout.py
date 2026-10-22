#!/usr/bin/env python3
"""Single-variable generation-layout probes in an owned GPU process only."""

import argparse
import hashlib
import importlib.util
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from bench_generation_end import asr, edit_distance, normalize, write_wav
from bench_tts_contention import TEXTS, gpu_snapshot, json_request
from generation_end_metrics import audio_stats, classify_stop

PROFILES = {
    "baseline": {},
    "text_step": {"non_streaming_mode": False},
    "xvector": {"xvec_only": True},
    "chunk2": {"streaming_chunk_size": 2},
}
HOLDOUT = [
    "我在这儿，你慢慢说吧。",
    "谢谢，谢谢，今天真的麻烦你了。",
    "提醒时间是明天上午八点零五分。",
    "你的意思是先保存，再退出，对吗？",
    "桌子上的小盒子，等一会儿再打开吧。",
    "如果你现在还不确定，我们可以先把问题记下来，等明天检查完结果以后，再一起决定下一步怎么做。",
]


def compute_pids():
    """Identify new GPU jobs without inspecting or controlling those jobs."""
    output = subprocess.check_output(
        ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits"],
        text=True,
    )
    return {int(line.strip()) for line in output.splitlines() if line.strip()}


def main(*, diagnostic_seeds=(16, 25), holdout_seeds=(4201, 4207)):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    allowed_pids = compute_pids() | {os.getpid()}
    before = gpu_snapshot()
    if (
        before["free_mib"] < 9000
        or before["utilization_pct"] > 20
        or json_request("http://127.0.0.1:11434", "/api/ps")["models"]
    ):
        raise RuntimeError(f"GPU not ready, no unload attempted: {before}")
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "audio").mkdir()
    os.environ.setdefault("HF_HOME", str(Path.home() / "voice-ai/cache/hf"))
    os.environ["HF_HUB_OFFLINE"] = "1"
    import faster_qwen3_tts.model as model_module
    import torch
    from faster_qwen3_tts import streaming
    from speech_to_speech.pipeline.messages import TTSInput

    torch.set_num_threads(4)
    module_path = args.repo / "bridge/voice_bridge.py"
    sys.path.insert(0, str(module_path.parent))
    spec = importlib.util.spec_from_file_location("owned_layout_bridge", module_path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    tload = time.perf_counter()
    handler = bridge._load_tts_handler()
    base = {
        key: getattr(handler, key)
        for key in ("streaming_chunk_size", "non_streaming_mode", "xvec_only")
    }
    manifest = {
        "scope": "isolated generation/handler; no additional audio cropping",
        "load_ms": (time.perf_counter() - tload) * 1000,
        "gpu_before": before,
        "allowed_gpu_pids": sorted(allowed_pids),
        "gpu_loaded": gpu_snapshot(),
        "base": base,
        "profiles": PROFILES,
        "diagnostic": {"texts": TEXTS, "seeds": list(diagnostic_seeds)},
        "holdout": {"texts": HOLDOUT, "seeds": list(holdout_seeds)},
        "warmups_per_profile": 2,
        "reference_sha256": hashlib.sha256(
            Path(handler.ref_audio).read_bytes()
        ).hexdigest(),
        "hashes": {},
    }
    for path in (
        Path(__file__),
        Path(streaming.__file__),
        Path(model_module.__file__),
        Path(sys.modules[type(handler).__module__].__file__),
        module_path,
    ):
        manifest["hashes"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path = args.out / "manifest.json"

    def save_manifest():
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    def configure(profile):
        for key, value in (base | PROFILES[profile]).items():
            setattr(handler, key, value)

    original_model = handler.model.generate_voice_clone_streaming
    original_stream = streaming.fast_generate_streaming
    original_sample = streaming.sample_logits
    trace = {}

    def sample(logits, **kwargs):
        token = original_sample(logits, **kwargs)
        trace["sampled"].append(token.detach().clone())
        return token

    def codec_stream(**kwargs):
        trace["eos_id"] = kwargs["config"].codec_eos_token_id
        trace["effective"] = {
            k: kwargs[k]
            for k in (
                "chunk_size",
                "max_new_tokens",
                "min_new_tokens",
                "temperature",
                "top_k",
                "top_p",
                "repetition_penalty",
                "do_sample",
            )
        }
        for codes, timing in original_stream(**kwargs):
            trace["codes"].append(codes.detach().clone())
            yield codes, timing

    def model_stream(*args, **kwargs):
        for item in original_model(*args, **kwargs):
            chunk, sr = handler._prepare_audio_chunk(item)
            if chunk is not None and sr is not None and chunk.size:
                values = np.asarray(chunk).reshape(-1).copy()
                trace["raw"].append(values)
                trace["raw_sr"] = sr
                trace["raw_events"].append(
                    {
                        "at_ms": (time.perf_counter() - trace["t0"]) * 1000,
                        "samples": values.size,
                        "voiced": bool(np.any(np.abs(values) >= 0.01)),
                    }
                )
            yield item

    def run(profile, phase, text_index, seed):
        nonlocal trace
        unexpected = compute_pids() - allowed_pids
        if unexpected:
            raise RuntimeError(
                f"New GPU job appeared; no unload attempted: {unexpected}"
            )
        configure(profile)
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        text = (TEXTS if phase == "diagnostic" else HOLDOUT)[text_index]
        key = f"{phase}-{profile}-t{text_index}-s{seed}"
        row = {
            "key": key,
            "profile": profile,
            "phase": phase,
            "text_index": text_index,
            "seed": seed,
        }
        trace = {
            "sampled": [],
            "codes": [],
            "raw": [],
            "raw_events": [],
            "t0": time.perf_counter(),
        }
        parts, events = [], []
        try:
            for chunk in handler.process(TTSInput(text=text, language_code="zh")):
                data = (
                    chunk
                    if isinstance(chunk, bytes)
                    else np.asarray(chunk, dtype="<i2").tobytes()
                )
                if not data:
                    continue
                events.append(
                    {
                        "at_ms": (time.perf_counter() - trace["t0"]) * 1000,
                        "samples": len(data) // 2,
                        "voiced": bool(
                            np.any(
                                np.abs(
                                    np.frombuffer(data, dtype="<i2").astype(np.int32)
                                )
                                >= 328
                            )
                        ),
                    }
                )
                parts.append(data)
            torch.cuda.synchronize()
            row["generation_ms"] = (time.perf_counter() - trace["t0"]) * 1000
            if not parts or not trace["codes"]:
                raise RuntimeError("Empty output or swallowed handler exception")
            pcm = b"".join(parts)
            raw = np.concatenate(trace["raw"])
            codes = torch.cat(trace["codes"]).cpu().numpy().astype("<i8")
            sampled = [int(t.item()) for t in trace["sampled"]]
            row.update(
                effective=trace["effective"],
                frames=len(codes),
                code_sha256=hashlib.sha256(codes.tobytes()).hexdigest(),
                stop_reason=classify_stop(
                    sampled,
                    trace["eos_id"],
                    len(codes),
                    trace["effective"]["max_new_tokens"],
                ),
                pcm=audio_stats(
                    np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768,
                    16000,
                    99 / 32768,
                ),
                raw=audio_stats(raw, trace["raw_sr"], 0.003),
                raw_sr=trace["raw_sr"],
                raw_events=trace["raw_events"],
                pcm_events=events,
                first_pcm_ms=events[0]["at_ms"],
                first_voiced_ms=next((e["at_ms"] for e in events if e["voiced"]), None),
                raw_first_ms=trace["raw_events"][0]["at_ms"],
                raw_first_voiced_ms=next(
                    (e["at_ms"] for e in trace["raw_events"] if e["voiced"]), None
                ),
            )
            write_wav(args.out / "audio" / f"{key}.wav", pcm, 16000)
            write_wav(
                args.out / "audio" / f"{key}-raw.wav",
                (np.clip(raw, -1, 1) * 32767).astype("<i2").tobytes(),
                trace["raw_sr"],
            )
            transcript = asr(pcm)
            row.update(
                asr_ok=True,
                asr_exact=normalize(transcript) == normalize(text),
                edit_distance=edit_distance(normalize(transcript), normalize(text)),
                asr_ending_match=normalize(transcript).endswith(normalize(text)[-4:]),
            )
            with (args.out / "transcripts.jsonl").open("a") as f:
                f.write(
                    json.dumps(
                        {"key": key, "expected": text, "heard": transcript},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception as exc:  # noqa: BLE001 - retain every failed measurement
            row.update(error=f"{type(exc).__name__}: {exc}", asr_ok=False)
        unexpected = compute_pids() - allowed_pids
        row["unexpected_gpu_pids_after"] = sorted(unexpected)
        with (args.out / "trials.jsonl").open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            "LAYOUT_TRIAL",
            key,
            row.get("stop_reason"),
            round(row.get("first_voiced_ms") or 0),
            round(row.get("pcm", {}).get("tail_ms") or 0),
            row.get("edit_distance"),
            row.get("error", ""),
            flush=True,
        )
        if unexpected:
            raise RuntimeError(f"GPU overlap retained in last trial: {unexpected}")

    try:
        save_manifest()
        if manifest["gpu_loaded"]["free_mib"] < 1500:
            raise RuntimeError("Less than 1500MiB free after owned load")
        for profile in PROFILES:
            configure(profile)
            for seed in (9998, 9999):
                torch.manual_seed(seed)
                list(handler.process(TTSInput(text=TEXTS[0], language_code="zh")))
        streaming.sample_logits = sample
        streaming.fast_generate_streaming = codec_stream
        handler.model.generate_voice_clone_streaming = model_stream
        for phase, texts, seeds, shuffle_seed in (
            ("diagnostic", TEXTS, diagnostic_seeds, 20261022),
            ("holdout", HOLDOUT, holdout_seeds, 20261023),
        ):
            grid = [
                (p, t, s) for p in PROFILES for t in range(len(texts)) for s in seeds
            ]
            random.Random(shuffle_seed).shuffle(grid)
            for profile, text_index, seed in grid:
                run(profile, phase, text_index, seed)
        manifest["completed"] = True
    finally:
        streaming.sample_logits = original_sample
        streaming.fast_generate_streaming = original_stream
        handler.model.generate_voice_clone_streaming = original_model
        handler.cleanup()
        save_manifest()


if __name__ == "__main__":
    main()
