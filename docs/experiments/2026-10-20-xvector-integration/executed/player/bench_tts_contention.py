#!/usr/bin/env python3
"""Measure the running TTS bridge under three controlled Ollama conditions.

No service restart or configuration change. Run on the GPU host, without a
second copy of the speech models. Generated audio stays in the output folder.
"""

import argparse
import hashlib
import http.client
import json
import math
import random
import subprocess
import threading
import time
import urllib.request
import wave
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np

TEXTS = [
    "好呀，我们开始吧。",
    "你是想吃火锅还是螺蛳粉呢？",
    "这个问题我想了一下，我们可以先从最简单的方法开始。",
    "不过我知道大概的原理，是因为阳光穿过大气层时，蓝光最容易被空气中的小颗粒散射开来。",
]
LOAD_PROMPT = (
    "请用中文分点详细解释语音识别、语言模型和语音合成如何协作，"
    "包括每个阶段的功能、数据形式和延迟来源。请写一篇至少一千字的说明。"
)


def json_request(base: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


def gpu_snapshot() -> dict:
    command = [
        "nvidia-smi",
        "--query-gpu=utilization.gpu,memory.used,memory.free,memory.total",
        "--format=csv,noheader,nounits",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    values = [int(x.strip()) for x in result.stdout.strip().split(",")]
    return dict(zip(["utilization_pct", "used_mib", "free_mib", "total_mib"], values))


class DecodeLoad:
    """A single owned generation request, stopped by closing its own stream."""

    def __init__(self, base: str, model: str):
        parsed = urlsplit(base)
        self.connection = http.client.HTTPConnection(
            parsed.hostname, parsed.port, timeout=60
        )
        self.model = model
        self.ready = threading.Event()
        self.stop_requested = threading.Event()
        self.first_at = None
        self.last_at = None
        self.ended_at = None
        self.error = None
        self.chunks = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        try:
            payload = {
                "model": self.model,
                "prompt": LOAD_PROMPT,
                "stream": True,
                "think": False,
                "keep_alive": "10m",
                "options": {
                    "num_ctx": 32768,
                    "num_predict": 512,
                    "temperature": 0,
                    "seed": 42,
                },
            }
            self.connection.request(
                "POST",
                "/api/generate",
                json.dumps(payload),
                {"Content-Type": "application/json"},
            )
            response = self.connection.getresponse()
            if response.status != 200:
                raise RuntimeError(f"Ollama returned {response.status}")
            while not self.stop_requested.is_set():
                line = response.readline()
                if not line:
                    break
                row = json.loads(line)
                if row.get("error"):
                    raise RuntimeError(row["error"])
                if row.get("response"):
                    self.last_at = time.perf_counter()
                    self.first_at = self.first_at or self.last_at
                    self.chunks += 1
                    self.ready.set()
                if row.get("done"):
                    break
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.ended_at = time.perf_counter()
            self.ready.set()
            self.connection.close()

    def start(self):
        self.thread.start()
        if not self.ready.wait(60) or self.first_at is None:
            self.stop()
            raise RuntimeError(self.error or "No LLM content received")

    def stop(self):
        self.stop_requested.set()
        self.thread.join(5)
        if self.thread.is_alive():
            self.connection.close()
            self.thread.join(5)
        if self.thread.is_alive():
            raise RuntimeError("Owned LLM request did not exit")


def measure_tts(base: str, text: str, destination: Path) -> dict:
    parsed = urlsplit(base)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=60)
    started = time.perf_counter()
    parts, pending = [], b""
    sample_count = 0
    first_byte_at = voiced_at = None
    first_voiced_sample = None
    peak = 0
    try:
        connection.request(
            "POST",
            "/api/tts/stream",
            json.dumps({"text": text}),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        headers_at = time.perf_counter()
        if response.status != 200:
            raise RuntimeError(
                f"TTS returned {response.status}: {response.read(200)!r}"
            )
        while True:
            data = response.read1(65536)
            received_at = time.perf_counter()
            if not data:
                break
            first_byte_at = first_byte_at or received_at
            parts.append(data)
            data = pending + data
            count = len(data) // 2 * 2
            pending = data[count:]
            samples = np.frombuffer(data[:count], dtype="<i2").astype(np.int32)
            if len(samples):
                peak = max(peak, int(np.abs(samples).max()))
                if voiced_at is None:
                    indices = np.flatnonzero(np.abs(samples) >= math.ceil(0.01 * 32768))
                    if len(indices):
                        voiced_at = received_at
                        first_voiced_sample = sample_count + int(indices[0])
                sample_count += len(samples)
        if pending or not sample_count or voiced_at is None:
            raise RuntimeError("Invalid, empty or silent PCM response")
        finished = time.perf_counter()
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"".join(parts))
        return {
            "headers_ms": round((headers_at - started) * 1000, 2),
            "first_byte_ms": round((first_byte_at - started) * 1000, 2),
            "first_voiced_received_ms": round((voiced_at - started) * 1000, 2),
            "total_ms": round((finished - started) * 1000, 2),
            "audio_s": round(sample_count / 16000, 4),
            "leading_silence_ms": round(first_voiced_sample / 16, 2),
            "peak_pcm": peak,
            "content_type": response.getheader("Content-Type"),
            "_started_at": started,
            "_voiced_at": voiced_at,
        }
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--bridge", default="http://127.0.0.1:8765")
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3.5-voice:latest")
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    args.out.mkdir(parents=True, exist_ok=True)
    manifest_file = args.out / "manifest.json"
    if manifest_file.exists():
        parser.error("output directory already contains an experiment")
    audio = args.out / "audio"
    audio.mkdir()
    initial_models = json_request(args.ollama, "/api/ps")
    if initial_models["models"]:
        raise RuntimeError(
            "Expected Ollama to be initially empty; no existing models were unloaded"
        )
    initial_gpu = gpu_snapshot()
    if initial_gpu["free_mib"] < 8500 or initial_gpu["utilization_pct"] > 20:
        raise RuntimeError(f"GPU not ready for a bounded experiment: {initial_gpu}")
    production = Path.home() / (
        "voice-ai/dsh-home/profiles/web/node_modules/"
        "@beiyege-01/dsh-voice-ai-girlfriend/lib/client.js"
    )
    manifest = {
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "production_bundle_sha256": hashlib.sha256(production.read_bytes()).hexdigest(),
        "initial_models": initial_models,
        "initial_gpu": initial_gpu,
        "initial_health": json_request(args.bridge, "/api/health"),
        "model": args.model,
        "texts": TEXTS,
        "repeats": args.repeats,
        "scope": "HTTP first voiced PCM received; excludes ASR, DSH and browser playback",
        "warmups": [],
    }
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    introduced_model = False
    counter = 0

    def warmup(phase):
        for i in range(2):
            row = measure_tts(args.bridge, TEXTS[0], audio / f"warmup-{phase}-{i}.wav")
            row = {k: v for k, v in row.items() if not k.startswith("_")}
            manifest["warmups"].append({"phase": phase, **row})

    def trial(condition, phase, repeat, text_index):
        nonlocal counter
        counter += 1
        row = {
            "trial_id": counter,
            "condition": condition,
            "phase": phase,
            "repeat": repeat,
            "text_index": text_index,
            "text": TEXTS[text_index],
        }
        load = None
        try:
            row["gpu_before"] = gpu_snapshot()
            if row["gpu_before"]["free_mib"] < 768:
                raise RuntimeError("Insufficient free VRAM")
            if condition == "decode":
                load = DecodeLoad(args.ollama, args.model)
                load.start()
            measured = measure_tts(
                args.bridge, TEXTS[text_index], audio / f"trial-{counter:03d}.wav"
            )
            if load is not None:
                load.stop()
                row["llm_chunks"] = load.chunks
                row["llm_error"] = load.error
                row["decode_spans_first_audio"] = (
                    load.first_at <= measured["_started_at"]
                    and load.last_at >= measured["_voiced_at"]
                )
            row.update({k: v for k, v in measured.items() if not k.startswith("_")})
            row["gpu_after"] = gpu_snapshot()
        except Exception as exc:
            row["error"] = str(exc)
        finally:
            if load is not None and load.thread.is_alive():
                load.stop()
            with (args.out / "trials.jsonl").open("a") as output:
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(json.dumps(row, ensure_ascii=False), flush=True)
        if row.get("error"):
            raise RuntimeError(row["error"])
        time.sleep(0.2)

    try:
        split = max(1, args.repeats // 2)
        warmup("unloaded-before")
        for repeat in range(split):
            for index in range(len(TEXTS)):
                trial("unloaded", "before", repeat, index)
        introduced_model = True
        json_request(
            args.ollama,
            "/api/generate",
            {
                "model": args.model,
                "prompt": "你好",
                "stream": False,
                "think": False,
                "keep_alive": "10m",
                "options": {"num_ctx": 32768, "num_predict": 1},
            },
        )
        manifest["resident_models"] = json_request(args.ollama, "/api/ps")
        warmup("resident")
        schedule = [
            (c, r, i)
            for r in range(args.repeats)
            for i in range(len(TEXTS))
            for c in ["resident", "decode"]
        ]
        random.Random(20261020).shuffle(schedule)
        for condition, repeat, index in schedule:
            trial(condition, "interleaved", repeat, index)
        json_request(
            args.ollama, "/api/generate", {"model": args.model, "keep_alive": 0}
        )
        introduced_model = False
        warmup("unloaded-after")
        for repeat in range(split, args.repeats):
            for index in range(len(TEXTS)):
                trial("unloaded", "after", repeat, index)
        manifest["completed"] = True
    finally:
        if introduced_model:
            json_request(
                args.ollama, "/api/generate", {"model": args.model, "keep_alive": 0}
            )
        manifest["final_models"] = json_request(args.ollama, "/api/ps")
        manifest["final_health"] = json_request(args.bridge, "/api/health")
        manifest["final_gpu"] = gpu_snapshot()
        manifest["final_bundle_sha256"] = hashlib.sha256(
            production.read_bytes()
        ).hexdigest()
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        print("Experiment finished; manifest saved.", flush=True)


if __name__ == "__main__":
    main()
