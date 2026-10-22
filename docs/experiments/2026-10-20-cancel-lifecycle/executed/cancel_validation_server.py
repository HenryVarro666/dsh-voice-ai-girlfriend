#!/usr/bin/env python3
"""ICL-only old/fixed cancellation comparison in the owned experiment process."""

import time

import xvector_validation_server as server
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from tts_cancel_lifecycle import StreamJob, cancellable_raw


class CancellationTTS(server.OwnedTTS):
    profiles = frozenset({"baseline", "fixed"})

    def load(self):
        super().load()
        original_raw = self.handler.model.generate_voice_clone_streaming

        def observed_raw(*args, **kwargs):
            row = server.CURRENT.get()
            inner = original_raw(*args, **kwargs)
            try:
                for part in inner:
                    row.setdefault("first_raw_ms", self.at(row))
                    row["raw_chunks"] = row.get("raw_chunks", 0) + 1
                    yield part
                row["raw_natural_end"] = True
            finally:
                inner.close()
                row["raw_exit_ms"] = self.at(row)

        self.handler.model.generate_voice_clone_streaming = cancellable_raw(
            observed_raw
        )
        original_endpoint = self.bridge.tts_stream

        async def endpoint(req, request):
            row = server.CURRENT.get()
            if row["profile"] == "baseline":
                return await original_endpoint(req, request)
            text = (req.text or "").strip()
            if not text:
                raise HTTPException(400, "Empty text")
            text = text[:512]

            def trace(event):
                row.setdefault(event + "_ms", self.at(row))
                self.save()

            def generate():
                for chunk in self.bridge._synthesize_iter(
                    self.handler, text, job.cancel
                ):
                    yield chunk.astype("<i2").tobytes()

            job = StreamJob(
                generate, self.infer_lock, self.bridge._tts_stream_tasks, trace
            )
            return StreamingResponse(
                job.body(),
                media_type="audio/L16;rate=16000;channels=1",
                headers={"X-Sample-Rate": "16000"},
            )

        self.bridge.tts_stream = endpoint
        self.manifest["scope"] = (
            "ICL both arms; baseline deployed endpoint; fixed private StreamJob + raw-chunk cancellation"
        )
        self.manifest["loaded_at_unix"] = time.time()
        (self.folder / "server-manifest.json").write_text(
            server.json.dumps(self.manifest, indent=2)
        )
        self.save()


if __name__ == "__main__":
    server.OwnedTTS = CancellationTTS
    server.main()
