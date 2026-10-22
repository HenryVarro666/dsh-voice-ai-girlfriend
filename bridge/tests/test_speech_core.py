"""CPU-only contracts: no FastAPI server, downloads or model weights required."""

import io
import sys
import unittest
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio_codec import decode_audio, pcm16_to_wav
from turn_detection import (
    TURN_SAMPLES,
    TextTurnJudge,
    TurnDetector,
    prepare_turn_audio,
    turn_tokens,
)


class AudioTests(unittest.TestCase):
    def test_pcm_wire_is_little_endian_and_normalized(self):
        samples = np.array([-32768, -1, 0, 1, 32767], dtype="<i2")
        decoded = decode_audio(samples.tobytes(), "application/octet-stream")
        np.testing.assert_array_equal(decoded, samples.astype(np.float32) / 32768)
        self.assertEqual(decoded.dtype, np.float32)

    def test_wav_round_trip_and_header(self):
        samples = np.array([-1234, 0, 5678], dtype=np.int16)
        body = pcm16_to_wav(samples)
        with wave.open(io.BytesIO(body)) as wav:
            self.assertEqual(
                (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()),
                (1, 2, 16000),
            )
        np.testing.assert_array_equal(
            decode_audio(body, "audio/wav"), samples / np.float32(32768)
        )

    def test_stereo_wav_is_downmixed_and_resampled(self):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(
                np.tile(np.array([1000, -1000], dtype="<i2"), 800).tobytes()
            )
        decoded = decode_audio(buf.getvalue(), "audio/wav")
        self.assertEqual(len(decoded), 1600)
        np.testing.assert_array_equal(decoded, np.zeros(1600))


class TurnTests(unittest.TestCase):
    def test_short_clip_is_left_padded(self):
        result = prepare_turn_audio(np.array([1, 2, 3], dtype=np.float32))
        self.assertEqual(len(result), TURN_SAMPLES)
        self.assertTrue(np.all(result[:-3] == 0))
        np.testing.assert_array_equal(result[-3:], [1, 2, 3])

    def test_long_clip_keeps_the_last_eight_seconds(self):
        data = np.arange(TURN_SAMPLES + 123, dtype=np.float32)
        np.testing.assert_array_equal(prepare_turn_audio(data), data[123:])

    def test_construction_and_empty_transcript_do_not_load_models(self):
        judge = TextTurnJudge(Path("/not/a/model"))
        self.assertFalse(judge.ready)
        self.assertEqual(judge.predict(" \n"), 0)
        self.assertFalse(judge.ready)
        self.assertFalse(TurnDetector(Path("/not/an/onnx")).ready)

    def test_chat_template_uses_end_token_not_trailing_whitespace(self):
        class Tokenizer:
            def apply_chat_template(self, messages, tokenize=False):
                return "<user>" + messages[0]["content"] + "<end>\n"

            def encode(self, value, add_special_tokens=False):
                return {
                    "<end>\n": [10, 20],
                    "。": [10],
                    "？": [11],
                    "！": [12],
                    "?": [13, 14],
                    "!": [12],
                }[value]

        prefix, ids = turn_tokens(Tokenizer())
        self.assertEqual(prefix, "<user>")
        self.assertEqual(ids, [10, 11, 12])


if __name__ == "__main__":
    unittest.main()
