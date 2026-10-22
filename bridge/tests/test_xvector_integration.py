"""Contracts for the private TTS seam and per-request lock instrumentation."""

import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import httpx
from fastapi.responses import Response
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from player_validation_proxy import create_app, upstream_headers
from xvector_validation_server import CURRENT, OwnedTTS, is_project_llm


class IntegrationSeamTests(unittest.TestCase):
    def test_owned_cache_cleanup_finishes_before_next_request_enters(self):
        async def run(folder):
            owner = OwnedTTS(folder, folder)
            clearing, release = threading.Event(), threading.Event()
            first = {"id": 1, "t0": time.perf_counter()}
            second = {"id": 2, "t0": time.perf_counter()}
            owner.rows.extend([first, second])

            def clear():
                clearing.set()
                if not release.wait(2):
                    raise RuntimeError("test cleanup release not signalled")

            owner.clear_cache = clear

            async def request(row):
                token = CURRENT.set(row)
                try:
                    async with owner.infer_lock:
                        pass
                finally:
                    CURRENT.reset(token)

            task1 = asyncio.create_task(request(first))
            while not clearing.is_set():
                await asyncio.sleep(0)
            task2 = asyncio.create_task(request(second))
            await asyncio.sleep(0)
            self.assertNotIn("lock_enter_ms", second)
            release.set()
            await asyncio.gather(task1, task2)
            self.assertGreaterEqual(second["lock_enter_ms"], first["lock_exit_ms"])
            self.assertIn("idle_cache_clear_ms", first)

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))

    def test_project_llama_server_is_identified_by_exact_model(self):
        args = [
            "/usr/local/lib/ollama/llama-server",
            "--model",
            "/models/frozen",
            "-c",
            "32768",
        ]
        self.assertTrue(is_project_llm(args, "/models/frozen"))
        self.assertFalse(is_project_llm(args, "/models/foreign"))
        self.assertFalse(is_project_llm(["python", *args[1:]], "/models/frozen"))
        self.assertFalse(is_project_llm(args[:2], "/models/frozen"))

    def test_loopback_origin_translation_does_not_admit_foreign_origins(self):
        headers = {
            "host": "127.0.0.1:18766",
            "origin": "http://127.0.0.1:18766",
            "cookie": "test_session=fixture",
        }
        own = upstream_headers(headers)
        self.assertEqual(own["origin"], "http://127.0.0.1:3080")
        self.assertEqual(own["cookie"], headers["cookie"])
        foreign = upstream_headers(headers | {"origin": "https://foreign.invalid"})
        self.assertEqual(foreign["origin"], "https://foreign.invalid")

    def test_only_test_tts_uses_owned_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "fixtures.json").write_text('{"fixtures":[],"inputs":[]}')
            client_js = folder / "client.js"
            client_js.write_text("// test")
            seen = []

            async def backend(request, profile):
                seen.append((profile, await request.json()))
                return Response(b"\x01\x00", media_type="audio/pcm")

            app = create_app(folder, client_js, tts_backend=backend)
            with TestClient(app) as client:
                response = client.post(
                    "/__voice_test__/tts/stream?policy=xvector",
                    json={"text": "你好"},
                )
                self.assertEqual(response.content, b"\x01\x00")
                self.assertEqual(seen, [("xvector", {"text": "你好"})])

    def test_auth_forwarding_and_test_marker_survive_backend_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "fixtures.json").write_text('{"fixtures":[],"inputs":[]}')
            app = create_app(folder, folder / "client.js", tts_backend=lambda *_: None)
            forwarded = []

            def upstream(request):
                forwarded.append(request.headers.get("cookie"))
                return httpx.Response(
                    200, text="<head></head>", headers={"Content-Type": "text/html"}
                )

            with TestClient(app) as client:
                client.portal.call(app.state.client.aclose)
                app.state.client = httpx.AsyncClient(
                    transport=httpx.MockTransport(upstream)
                )
                response = client.get("/", headers={"Cookie": "test_session=fixture"})
                self.assertIn('data-xvector="1"', response.text)
                self.assertEqual(forwarded, ["test_session=fixture"])

    def test_waiting_request_keeps_its_own_trace_and_lock_order(self):
        async def run(folder):
            owner = OwnedTTS(folder, folder)
            ready, release = asyncio.Event(), asyncio.Event()
            first = {"t0": time.perf_counter(), "id": 1}
            second = {"t0": time.perf_counter(), "id": 2}
            owner.rows.extend([first, second])

            async def one():
                token = CURRENT.set(first)
                try:
                    async with owner.infer_lock:
                        ready.set()
                        await release.wait()
                finally:
                    CURRENT.reset(token)

            async def two():
                token = CURRENT.set(second)
                try:
                    async with owner.infer_lock:
                        pass
                finally:
                    CURRENT.reset(token)

            task1 = asyncio.create_task(one())
            await ready.wait()
            task2 = asyncio.create_task(two())
            await asyncio.sleep(0)
            self.assertNotIn("lock_enter_ms", second)
            release.set()
            await asyncio.gather(task1, task2)
            self.assertGreaterEqual(second["lock_enter_ms"], first["lock_exit_ms"])
            self.assertEqual(
                len(json.loads((folder / "server-streams.json").read_text())), 2
            )

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
