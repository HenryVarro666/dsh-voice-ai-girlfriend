#!/usr/bin/env python3
"""Loopback-only DSH test proxy: preserves auth, injects a test UI, wraps TTS."""

import argparse
import asyncio
import json
import sys
import time
import wave
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
import uvicorn
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from stream_tail import TailGuard

HOP = {
    "host",
    "connection",
    "upgrade",
    "content-length",
    "content-encoding",
    "transfer-encoding",
}


def upstream_headers(headers):
    """Translate this loopback proxy's own Origin; keep foreign origins and auth."""
    outgoing = {k: v for k, v in headers.items() if k not in HOP}
    if headers.get("origin") == f"http://{headers.get('host')}":
        outgoing["origin"] = "http://127.0.0.1:3080"
    return outgoing


def create_app(folder, client_js, tts_backend=None):
    fixtures = json.loads((folder / "fixtures.json").read_text())
    audio_files = {
        x["id"]: folder / "audio" / x["file"]
        for x in fixtures["fixtures"] + fixtures["inputs"]
    }
    counter = max(
        (
            int(p.name.split("-")[1])
            for p in (folder / "streams").glob("stream-*-original.wav")
        ),
        default=0,
    )

    @asynccontextmanager
    async def lifespan(app):
        app.state.client = httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=10), trust_env=False
        )
        try:
            yield
        finally:
            await app.state.client.aclose()

    app = FastAPI(lifespan=lifespan)

    @app.get("/__voice_test__/harness.js")
    async def harness():
        return FileResponse(
            client_js,
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/__voice_test__/fixtures.json")
    async def manifest():
        return fixtures

    @app.get("/__voice_test__/review")
    async def review():
        return FileResponse(folder / "review.html", media_type="text/html")

    @app.get("/__voice_test__/audio/{key}.wav")
    async def audio(key: str):
        path = audio_files.get(key)
        return (
            FileResponse(path, media_type="audio/wav")
            if path
            else Response(status_code=404)
        )

    @app.post("/__voice_test__/results/{kind}")
    async def results(kind: str, request: Request):
        if kind not in {"player", "e2e", "interruption"}:
            return Response(status_code=400)
        data = await request.json()
        # Only this test tab writes these generic experiment results.
        (folder / f"browser-{kind}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2)
        )
        return {"saved": True}

    async def pcm_response(source, policy, key):
        nonlocal counter
        if policy not in {"baseline", "guard99", "guard3"}:
            return Response(status_code=400)
        counter += 1
        stream_id = f"stream-{counter:04d}"
        directory = folder / "streams"
        directory.mkdir(exist_ok=True)

        async def body():
            guard = (
                None
                if policy == "baseline"
                else TailGuard(99 if policy == "guard99" else 3)
            )
            started = time.perf_counter()
            record = {
                "stream_id": stream_id,
                "policy": policy,
                "key": key,
                "packets": [],
            }
            carry = b""
            original = wave.open(str(directory / f"{stream_id}-original.wav"), "wb")
            output = wave.open(str(directory / f"{stream_id}-output.wav"), "wb")
            for wav in [original, output]:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
            try:
                async for block in source:
                    block = carry + block
                    aligned = len(block) & ~1
                    carry, block = block[aligned:], block[:aligned]
                    if not block:
                        continue
                    original.writeframesraw(block)
                    for part in guard.feed(block) if guard else [block]:
                        output.writeframesraw(part)
                        record["packets"].append(
                            {
                                "at_ms": (time.perf_counter() - started) * 1000,
                                "samples": len(part) // 2,
                            }
                        )
                        yield part
                if carry:
                    raise ValueError("upstream ended mid-sample")
                if guard:
                    for part in guard.finish():
                        output.writeframesraw(part)
                        record["packets"].append(
                            {
                                "at_ms": (time.perf_counter() - started) * 1000,
                                "samples": len(part) // 2,
                            }
                        )
                        yield part
                record["completed"] = True
            finally:
                if guard:
                    record.update(
                        removed_ms=guard.removed_samples / 16,
                        peak_pending_bytes=guard.peak_pending_bytes,
                        overflows=guard.overflows,
                    )
                    guard.close()
                original.close()
                output.close()
                record["elapsed_ms"] = (time.perf_counter() - started) * 1000
                with (folder / "proxy-streams.jsonl").open("a") as out:
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                await source.aclose()

        return StreamingResponse(
            body(),
            media_type="audio/pcm;rate=16000;channels=1;format=s16le",
            headers={"X-Sample-Rate": "16000", "X-Test-Stream": stream_id},
        )

    @app.get("/__voice_test__/fixture/{key}")
    async def fixture(key: str, policy: str = "baseline"):
        if key not in audio_files:
            return Response(status_code=404)

        async def source():
            with wave.open(str(audio_files[key])) as wav:
                index = 0
                while block := wav.readframes(512):
                    if key == "late-packet" and index == 2:
                        await asyncio.sleep(0.8)
                    yield block
                    index += 1
                    await asyncio.sleep(0.008)

        return await pcm_response(source(), policy, key)

    @app.post("/__voice_test__/tts/stream")
    async def tts(request: Request, policy: str = "baseline"):
        if tts_backend is not None:
            return await tts_backend(request, policy)
        upstream = await app.state.client.send(
            app.state.client.build_request(
                "POST",
                "http://127.0.0.1:8765/api/tts/stream",
                content=await request.body(),
                headers={"Content-Type": "application/json"},
            ),
            stream=True,
        )
        if upstream.status_code != 200:
            data = await upstream.aread()
            await upstream.aclose()
            return Response(data, status_code=upstream.status_code)

        async def source():
            try:
                async for block in upstream.aiter_bytes(1024):
                    yield block
            finally:
                await upstream.aclose()

        return await pcm_response(source(), policy, "live-tts")

    @app.websocket("/{path:path}")
    async def websocket_proxy(client: WebSocket, path: str):
        headers = {
            k: v
            for k, v in upstream_headers(client.headers).items()
            if not k.startswith("sec-websocket")
        }
        protocols = [
            p.strip()
            for p in client.headers.get("sec-websocket-protocol", "").split(",")
            if p.strip()
        ]
        url = "ws://127.0.0.1:3080/" + path
        if client.url.query:
            url += "?" + client.url.query
        async with websockets.connect(
            url,
            additional_headers=headers,
            subprotocols=protocols or None,
            proxy=None,
            max_size=None,
        ) as upstream:
            await client.accept(subprotocol=upstream.subprotocol)

            async def outgoing():
                while True:
                    message = await client.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    await upstream.send(
                        message.get("text")
                        if message.get("text") is not None
                        else message["bytes"]
                    )

            async def incoming():
                async for message in upstream:
                    if isinstance(message, str):
                        await client.send_text(message)
                    else:
                        await client.send_bytes(message)

            tasks = [asyncio.create_task(outgoing()), asyncio.create_task(incoming())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                with suppress(RuntimeError, WebSocketDisconnect):
                    await client.close()

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )
    async def proxy(request: Request, path: str):
        url = "http://127.0.0.1:3080/" + path
        if request.url.query:
            url += "?" + request.url.query
        headers = upstream_headers(request.headers)
        upstream = await app.state.client.send(
            app.state.client.build_request(
                request.method, url, headers=headers, content=await request.body()
            ),
            stream=True,
        )
        outgoing = {k: v for k, v in upstream.headers.items() if k not in HOP}
        if "text/html" in outgoing.get("content-type", ""):
            content = (await upstream.aread()).decode()
            await upstream.aclose()
            if upstream.status_code == 200:
                content = content.replace(
                    "<head>",
                    '<head><script src="/__voice_test__/harness.js"'
                    + (' data-xvector="1"' if tts_backend else "")
                    + "></script>",
                    1,
                )
            return HTMLResponse(
                content, status_code=upstream.status_code, headers=outgoing
            )
        return StreamingResponse(
            upstream.aiter_bytes(),
            status_code=upstream.status_code,
            headers=outgoing,
            background=BackgroundTask(upstream.aclose),
        )

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--client-js", type=Path, required=True)
    args = parser.parse_args()
    uvicorn.run(
        create_app(args.folder, args.client_js),
        host="127.0.0.1",
        port=8766,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
