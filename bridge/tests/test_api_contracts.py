"""HTTP contract smoke tests with fake model output, not model benchmarks."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
from fastapi.testclient import TestClient

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "media": {
                        "bg_images_dir": str(root / "bg"),
                        "task_videos_dir": str(root / "tasks"),
                    },
                    "stt": {},
                    "tts": {},
                    "voices_dir": str(root / "voices"),
                    "turn": {"enabled": False},
                    "digital_human": {"enabled": False},
                }
            )
        )
        spec = importlib.util.spec_from_file_location(
            "bridge_under_test", BRIDGE / "voice_bridge.py"
        )
        cls.bridge = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"VOICE_BRIDGE_CONFIG": str(config)}):
            spec.loader.exec_module(cls.bridge)
        # No TestClient context manager: background GPU warmups are deliberately not run.
        cls.client = TestClient(cls.bridge.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.temp.cleanup()

    def test_health_requires_no_model_weights(self):
        result = self.client.get("/api/health")
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["stt"])
        self.assertFalse(result.json()["turn"])

    def test_disabled_turn_gives_explicit_fallback(self):
        self.assertEqual(
            self.client.post("/api/turn", content=b"\0\0").status_code, 404
        )

    def test_empty_stt_is_rejected_before_loading(self):
        self.assertEqual(self.client.post("/api/stt", content=b"").status_code, 400)

    def test_stream_labels_little_endian_pcm_and_preserves_bytes(self):
        samples = np.array([-32768, 32767], dtype="<i2")
        with (
            patch.object(
                self.bridge.models, "ensure_tts", AsyncMock(return_value=object())
            ),
            patch.object(self.bridge, "_synthesize_iter", return_value=iter([samples])),
        ):
            response = self.client.post("/api/tts/stream", json={"text": "你好"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("format=s16le", response.headers["content-type"])
        self.assertEqual(response.headers["x-sample-rate"], "16000")
        self.assertEqual(response.content, samples.tobytes())
