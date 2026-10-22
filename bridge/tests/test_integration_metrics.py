"""Guard clip attribution and the speech-end versus TTS timing boundary."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from integration_metrics import cancel_metrics, e2e_metrics, queue_metrics


class IntegrationMetricsTests(unittest.TestCase):
    def test_skipped_waiter_has_no_fake_producer_time(self):
        row = {
            "server": [
                {
                    "segment": "1",
                    "worker_exit_ms": 6,
                    "consumer_exit_ms": 5,
                    "queue_skipped_ms": 5,
                }
            ],
            "stopped_segment": 1,
            "sources": [],
            "stop_requested_at": 100,
            "late_sources_in_stopped_segment": 0,
        }
        result = cancel_metrics(row)
        self.assertTrue(result["queue_skipped"])
        self.assertFalse(result["model_started"])
        self.assertEqual(result["cancelled_request_computed_after_lock"], 0)
        self.assertEqual(result["consumer_close_to_producer_exit_ms"], 1)
        self.assertFalse(result["lock_exit_recorded"])

    def test_queue_uses_pcm_counts_even_when_global_segment_label_is_wrong(self):
        row = {
            "started": 0,
            "last_ended_at": 100,
            "server": [
                {"stream_id": "a", "sent_samples": 160},
                {"stream_id": "b", "sent_samples": 160},
            ],
            "sources": [
                {
                    "segment": 2,
                    "called_at": i,
                    "frames": 160,
                    "sample_rate": 16000,
                    "scheduled_start": i * 50,
                    "scheduled_end": i * 50 + 10,
                }
                for i in (0, 1)
            ],
        }
        bounds = {
            key: {"samples": 160, "first_voiced_sample": 0, "last_voiced_sample": 159}
            for key in ("a", "b")
        }
        result = queue_metrics(row, bounds)
        self.assertEqual(result["received_samples"], 320)
        self.assertEqual(result["interclip_schedule_gap_ms"], 40)
        row["sources"][0]["frames"] = 161
        with self.assertRaises(ValueError):
            queue_metrics(row, bounds)

    def test_pre_first_cancel_without_playback_has_no_negative_stop_time(self):
        row = {
            "server": [
                {
                    "segment": "1",
                    "producer_enter_ms": 0,
                    "producer_exit_ms": 2000,
                    "consumer_exit_ms": 10,
                    "lock_exit_ms": 2001,
                }
            ],
            "stopped_segment": 1,
            "sources": [],
            "stop_requested_at": 5000,
            "late_sources_in_stopped_segment": 0,
            "sources_before_stop": 0,
        }
        result = cancel_metrics(row)
        self.assertIsNone(result["playback_stop_ms"])
        self.assertEqual(result["consumer_close_to_producer_exit_ms"], 1990)

    def test_tts_first_sound_is_not_full_voice_to_voice(self):
        row = {
            "events": [
                {"api": "turn", "result": {"complete": True}},
                {"api": "tts/stream", "at": 1100, "text": "你好"},
            ],
            "input_end": 100,
            "first_voice_at": 1300,
            "server": [{"sent_samples": 100}],
            "sources": [{"frames": 100}],
        }
        metrics = e2e_metrics(row)
        self.assertEqual(metrics["voice_to_voice_ms"], 1200)
        self.assertEqual(metrics["tts_to_voice_ms"], 200)
        self.assertEqual(metrics["before_tts_ms"], 1000)


if __name__ == "__main__":
    unittest.main()
