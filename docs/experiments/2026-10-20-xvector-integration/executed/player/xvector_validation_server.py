#!/usr/bin/env python3
"""Run the actual deployed TTS endpoint in an owned process behind a DSH proxy."""

import argparse
import asyncio
import contextvars
import hashlib
import importlib.util
import json
import os
import sys
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from bench_generation_layout import compute_pids
from bench_tts_contention import gpu_snapshot, json_request
from fastapi import HTTPException
from player_validation_proxy import create_app

CURRENT = contextvars.ContextVar("experiment_request")


class TimedLock:
    def __init__(self, owner):
        self.lock = asyncio.Lock()
        self.owner = owner

    async def __aenter__(self):
        await self.lock.acquire()
        CURRENT.get()["lock_enter_ms"] = self.owner.at(CURRENT.get())

    async def __aexit__(self, *exc):
        row = CURRENT.get()
        row["lock_exit_ms"] = self.owner.at(row)
        self.lock.release()
        self.owner.save()


class OwnedTTS:
    def __init__(self, repo, folder):
        self.repo, self.folder = repo, folder
        self.rows, self.handler = [], None
        self.infer_lock = TimedLock(self)

    @staticmethod
    def at(row):
        return (time.perf_counter() - row["t0"]) * 1000

    def save(self):
        (self.folder / "server-streams.json").write_text(
            json.dumps(self.rows, ensure_ascii=False, indent=2)
        )

    def load(self):
        os.environ.setdefault("HF_HOME", str(Path.home() / "voice-ai/cache/hf"))
        os.environ["HF_HUB_OFFLINE"] = "1"
        import torch
        from speech_to_speech.pipeline.messages import TTSInput

        before = gpu_snapshot()
        if before["free_mib"] < 9000 or before["utilization_pct"] > 20:
            raise RuntimeError(f"No safe private-model window: {before}")
        if json_request("http://127.0.0.1:11434", "/api/ps")["models"]:
            raise RuntimeError("Existing Ollama model; no unload attempted")
        self.allowed = compute_pids() | {os.getpid()}
        torch.set_num_threads(4)
        sys.path.insert(0, str(self.repo / "bridge"))
        source = self.repo / "bridge/voice_bridge.py"
        spec = importlib.util.spec_from_file_location("private_test_bridge", source)
        self.bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bridge)
        self.handler = self.bridge._load_tts_handler()
        for profile in (False, True):
            self.handler.xvec_only = profile
            for seed in (9998, 9999):
                torch.manual_seed(seed)
                list(
                    self.handler.process(
                        TTSInput(text="你好，今天就先这样吧。", language_code="zh")
                    )
                )
        self.handler.xvec_only = False
        cached = gpu_snapshot()
        torch.cuda.empty_cache()  # This owned process only; production is untouched.
        ready = gpu_snapshot()
        self.bridge.models = self
        original_iter = self.bridge._synthesize_iter

        def traced(handler, text, cancel=None):
            row = CURRENT.get()
            handler.xvec_only = row["profile"] == "xvector"
            torch.manual_seed(row["seed"])
            row["producer_enter_ms"] = self.at(row)
            try:
                for part in original_iter(handler, text, cancel):
                    row.setdefault("first_pcm_ms", self.at(row))
                    row["produced_samples"] = row.get("produced_samples", 0) + len(part)
                    yield part
            finally:
                row["producer_exit_ms"] = self.at(row)
                row["cancel_set_at_exit"] = bool(cancel and cancel.is_set())

        self.bridge._synthesize_iter = traced
        self.manifest = {
            "ready": True,
            "pid": os.getpid(),
            "gpu_before": before,
            "gpu_before_empty_cache": cached,
            "gpu_ready": ready,
            "allowed_gpu_pids": sorted(self.allowed),
            "bridge_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "reference_sha256": hashlib.sha256(
                Path(self.handler.ref_audio).read_bytes()
            ).hexdigest(),
            "tts_defaults": {
                k: getattr(self.handler, k)
                for k in ("streaming_chunk_size", "non_streaming_mode", "xvec_only")
            },
            "scope": "Actual deployed tts_stream and synthesize_iter, privately loaded; no tail wrapper",
        }
        (self.folder / "server-manifest.json").write_text(
            json.dumps(self.manifest, indent=2)
        )

    async def ensure_tts(self):
        return self.handler

    async def stream(self, request, profile):
        if profile not in {"baseline", "xvector"}:
            raise HTTPException(400, "Unknown test profile")
        extra = compute_pids() - self.allowed
        if extra:
            ps = json_request("http://127.0.0.1:11434", "/api/ps")["models"]
            # Admit only this project's normally loaded LLM, never another GPU job.
            if len(ps) != 1 or ps[0]["name"] != "qwen3.5-voice:latest":
                raise HTTPException(409, "Unexpected model or GPU job")
            for pid in extra:
                if "ollama" not in Path(f"/proc/{pid}/comm").read_text().lower():
                    raise HTTPException(409, "External GPU job; no unload attempted")
            self.allowed |= extra
        if gpu_snapshot()["free_mib"] < 512:
            raise HTTPException(409, "Private experiment memory margin below 512MiB")
        body = await request.json()
        row = {
            "stream_id": f"owned-{len(self.rows) + 1:04d}",
            "profile": profile,
            "trial": request.headers.get("X-Experiment-Trial", "unknown"),
            "seed": int(request.headers.get("X-Experiment-Seed", "7001")),
            "text_sha256": hashlib.sha256(body.get("text", "").encode()).hexdigest(),
            "chars": len(body.get("text", "")),
            "segment": request.headers.get("X-Experiment-Segment", "0"),
            "t0": time.perf_counter(),
        }
        self.rows.append(row)
        token = CURRENT.set(row)
        try:
            response = await self.bridge.tts_stream(
                self.bridge.TTSRequest(**body), request
            )
        finally:
            CURRENT.reset(token)
        original_body = response.body_iterator

        async def observed_body():
            directory = self.folder / "streams"
            directory.mkdir(exist_ok=True)
            with wave.open(str(directory / f"{row['stream_id']}.wav"), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                try:
                    async for data in original_body:
                        wav.writeframesraw(data)
                        row.setdefault("first_sent_ms", self.at(row))
                        row["sent_samples"] = (
                            row.get("sent_samples", 0) + len(data) // 2
                        )
                        yield data
                    row["consumer_completed"] = True
                finally:
                    await original_body.aclose()
                    row["consumer_exit_ms"] = self.at(row)
                    self.save()

        response.body_iterator = observed_body()
        response.headers["X-Test-Stream"] = row["stream_id"]
        return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--folder", required=True, type=Path)
    parser.add_argument("--client-js", required=True, type=Path)
    args = parser.parse_args()
    backend = OwnedTTS(args.repo, args.folder)
    app = create_app(args.folder, args.client_js, tts_backend=backend.stream)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(backend.load)
        async with original_lifespan(app):
            yield
        if backend.handler:
            backend.handler.cleanup()

    app.router.lifespan_context = lifespan

    @app.get("/__voice_test__/state")
    async def state():
        return {
            "streams": backend.rows,
            "pending": len(backend.bridge._tts_stream_tasks),
            "gpu": gpu_snapshot(),
        }

    # Register the diagnostic state route ahead of the generic DSH forwarding route.
    app.router.routes.insert(0, app.router.routes.pop())
    uvicorn.run(app, host="127.0.0.1", port=8766, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
