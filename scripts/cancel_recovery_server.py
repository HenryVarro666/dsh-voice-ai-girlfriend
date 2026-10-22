#!/usr/bin/env python3
"""Private v1/v2 recovery probes; faults and slow sends exist only here."""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import uvicorn
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from player_validation_proxy import create_app
from tts_cancel_lifecycle import JobResponse, StreamJob, cancellable_raw
from xvector_validation_server import CURRENT, OwnedTTS


class FaultIterator:
    """Inject outside the handler, which otherwise catches model exceptions."""

    def __init__(self, inner, fault):
        self.inner, self.fault, self.count = inner, fault, 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.fault == "generate" and self.count == 3:
            raise RuntimeError("injected generation failure")
        self.count += 1
        return next(self.inner).astype("<i2").tobytes()

    def close(self):
        self.inner.close()
        if self.fault == "close":
            raise RuntimeError("injected close failure")


class RecoveryTTS(OwnedTTS):
    profiles = frozenset({"v1", "fixed"})

    def __init__(self, repo, folder, v1_source):
        super().__init__(repo, folder)
        self.v1_source, self.jobs = v1_source, {}
        self.active, self.max_active = 0, 0

    def load(self):
        file_handler = logging.handlers.RotatingFileHandler

        def private_log(filename, *args, **kwargs):
            if Path(filename) == self.repo / "bridge/logs/bridge.log":
                filename = self.folder / "private-bridge.log"
            return file_handler(filename, *args, **kwargs)

        with patch("logging.handlers.RotatingFileHandler", private_log):
            super().load()
        spec = importlib.util.spec_from_file_location(
            "frozen_cancel_v1", self.v1_source
        )
        self.v1 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.v1)
        original_raw = self.handler.model.generate_voice_clone_streaming

        def raw(*args, **kwargs):
            row = CURRENT.get()
            inner = original_raw(*args, **kwargs)
            try:
                for part in inner:
                    row["raw_chunks"] = row.get("raw_chunks", 0) + 1
                    row.setdefault("first_raw_ms", self.at(row))
                    yield part
            finally:
                inner.close()
                row["raw_exit_ms"] = self.at(row)

        self.handler.model.generate_voice_clone_streaming = self.v1.cancellable_raw(
            cancellable_raw(raw)
        )
        original_iter = self.bridge._synthesize_iter

        def tracked(*args):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                yield from original_iter(*args)
            finally:
                self.active -= 1

        self.bridge._synthesize_iter = tracked

        async def endpoint(req, request):
            row = CURRENT.get()
            text = (req.text or "").strip()[:512]
            fault = request.headers.get("X-Experiment-Fault", "none")
            delay = int(request.headers.get("X-Experiment-Send-Delay-Ms", "0"))
            if (
                not text
                or fault not in {"none", "generate", "close"}
                or not 0 <= delay <= 100
            ):
                raise HTTPException(400, "Invalid private probe")
            row.update(fault=fault, send_delay_ms=delay)

            def trace(event):
                row.setdefault(event + "_ms", self.at(row))
                self.save()

            def generate():
                return FaultIterator(
                    self.bridge._synthesize_iter(self.handler, text, job.cancel), fault
                )

            cls = self.v1.StreamJob if row["profile"] == "v1" else StreamJob
            job = cls(generate, self.infer_lock, self.bridge._tts_stream_tasks, trace)
            self.jobs[row["stream_id"]] = job
            put = job.chunks.put_nowait

            def observed_put(part):
                put(part)
                row["queue_peak"] = max(row.get("queue_peak", 0), job.chunks.qsize())

            job.chunks.put_nowait = observed_put

            def completed(task):
                row["max_put_wait_ms"] = getattr(job, "max_put_wait_ms", None)
                error = task.exception() if not task.cancelled() else None
                error = error or getattr(job, "error", None)
                row["worker_error"] = str(error) if error else None
                row["worker_done_ms"] = self.at(row)
                self.jobs.pop(row["stream_id"], None)
                self.save()

            job.task.add_done_callback(completed)
            response = (
                StreamingResponse(
                    job.body(), media_type="audio/L16;rate=16000;channels=1"
                )
                if row["profile"] == "v1"
                else JobResponse(job)
            )
            send_stream = response.stream_response

            async def delayed_stream(send):
                async def delayed_send(message):
                    if delay and message.get("body"):
                        await asyncio.sleep(delay / 1000)
                    await send(message)

                await send_stream(delayed_send)

            response.stream_response = delayed_stream
            return response

        self.bridge.tts_stream = endpoint
        self.manifest.update(
            scope="Frozen cancellation v1 versus bounded/disconnect-safe v2; ICL both arms; no crop",
            v1_sha256=hashlib.sha256(self.v1_source.read_bytes()).hexdigest(),
        )
        (self.folder / "server-manifest.json").write_text(
            json.dumps(self.manifest, indent=2)
        )

    async def close(self):
        for job in list(self.jobs.values()):
            job.stop()
        await asyncio.gather(
            *(job.task for job in list(self.jobs.values())), return_exceptions=True
        )
        self.save()
        self.handler.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "folder", "client-js", "v1-source"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    backend = RecoveryTTS(args.repo, args.folder, args.v1_source)
    app = create_app(args.folder, args.client_js, tts_backend=backend.stream)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(backend.load)
        try:
            async with original_lifespan(app):
                yield
        finally:
            await backend.close()

    app.router.lifespan_context = lifespan

    @app.get("/__voice_test__/state")
    async def state():
        return {
            "streams": backend.rows,
            "pending": len(backend.bridge._tts_stream_tasks),
            "active": backend.active,
            "max_active": backend.max_active,
        }

    app.router.routes.insert(0, app.router.routes.pop())

    @app.post("/__voice_test__/worker-cancel/{stream_id}")
    async def worker_cancel(stream_id: str):
        job = backend.jobs.get(stream_id)
        row = next((r for r in backend.rows if r["stream_id"] == stream_id), None)
        if job is None or row["profile"] != "fixed":
            raise HTTPException(409, "Only an active owned v2 worker may be cancelled")
        row["forced_cancel_ms"] = backend.at(row)
        row["phase_at_forced_cancel"] = job.phase
        job.task.cancel()
        return {"phase": job.phase}

    app.router.routes.insert(0, app.router.routes.pop())
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8766,
        access_log=False,
        log_level="warning",
        timeout_graceful_shutdown=2,
    )


if __name__ == "__main__":
    main()
