#!/usr/bin/env python3
"""Prepare synthetic weak-ending fixtures using the running bridge only."""

import argparse
import hashlib
import io
import json
import sys
import urllib.request
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from stream_tail import TailGuard

TEXTS = [
    "好呀，今天就先这样吧。",
    "不着急，你慢慢说就好。",
    "我在这里陪着你呢。",
    "可以，记得早点休息哦。",
    "好的，我们明天再聊吧。",
    "请记住，最后的数字是七二九。",
]
INPUTS = [
    "你好，请用一句简短的话和我打招呼。",
    "你吃饭了吗？",
    "给我讲一个很短的笑话吧。",
    "我想问一下那个",
    "明天出门要不要带伞？",
    "等等，我想换个话题聊聊。",
]


def read_wav(raw):
    with wave.open(io.BytesIO(raw)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").copy()


def wav_bytes(samples):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(samples.astype("<i2").tobytes())
    return stream.getvalue()


def synth(text):
    request = urllib.request.Request(
        "http://127.0.0.1:8765/api/tts",
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return read_wav(response.read())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if (args.out / "fixtures.json").exists():
        parser.error("fixture manifest already exists")
    audio = args.out / "audio"
    audio.mkdir(exist_ok=True)
    manifest = {
        "scope": "Synthetic and controlled attenuation, no natural human whisper",
        "fixtures": [],
        "inputs": [],
        "guard_sha256": hashlib.sha256(
            Path(__file__)
            .resolve()
            .parents[1]
            .joinpath("bridge/stream_tail.py")
            .read_bytes()
        ).hexdigest(),
    }
    checks = []

    def save(key, samples, **metadata):
        path = audio / f"{key}.wav"
        path.write_bytes(wav_bytes(samples))
        item = {"id": key, "file": path.name, "samples": len(samples), **metadata}
        manifest["fixtures"].append(item)
        return item

    for index, text in enumerate(TEXTS):
        original = synth(text)
        save(f"normal-{index}", original, kind="natural_tts", text=text)
        voice = np.flatnonzero(np.abs(original.astype(np.int32)) >= 328)
        end = int(voice[-1]) + 1
        start = max(int(voice[0]) + 800, end - 19200)
        weak = original[:end].copy()
        region = weak[start:].astype(np.float64)
        region *= 70 / max(1, np.abs(region).max())
        weak[start:] = np.rint(region).astype(np.int16)
        weak = np.concatenate([weak, np.zeros(19200, dtype=np.int16)])
        save(
            f"soft-{index}",
            weak,
            kind="controlled_weak_ending",
            text=text,
            weak_start=start,
            weak_end=end,
            tail_peak=70,
        )
        for policy, threshold in [("baseline", None), ("guard99", 99), ("guard3", 3)]:
            guard = TailGuard(threshold) if threshold is not None else None
            data = weak.astype("<i2").tobytes()
            output = (
                b"".join(
                    guard.process(data[i : i + 1024] for i in range(0, len(data), 1024))
                )
                if guard
                else data
            )
            count = len(output) // 2
            lost_voice = np.count_nonzero(
                np.abs(original[max(count, start) : end].astype(np.int32)) >= 328
            )
            checks.append(
                {
                    "id": f"soft-{index}",
                    "policy": policy,
                    "removed_ms": (len(weak) - count) / 16,
                    "known_voice_samples_lost": int(lost_voice),
                    "prefix_preserved": output == data[: len(output)],
                }
            )
            (audio / f"soft-{index}-{policy}.wav").write_bytes(
                wav_bytes(np.frombuffer(output, dtype="<i2"))
            )
            tail = np.frombuffer(output, dtype="<i2")[start:]
            # Only the review suffix is normalized; timings use original PCM.
            peak = max(1, np.abs(tail.astype(np.int32)).max(initial=0))
            loud = (
                np.rint(tail.astype(np.float64) * 22000 / peak)
                .clip(-32768, 32767)
                .astype(np.int16)
            )
            (audio / f"review-{index}-{policy}.wav").write_bytes(wav_bytes(loud))
        print("FIXTURE", index, flush=True)
    archived = (
        Path.home()
        / "voice-ai/experiments/20261020-tts-tail/main-01/audio/main-025.wav"
    )
    save("long-tail", read_wav(archived.read_bytes()), kind="archived_model_tail")
    normal = read_wav((audio / "normal-1.wav").read_bytes())
    voiced = np.flatnonzero(np.abs(normal.astype(np.int32)) >= 328)
    half = (int(voiced[0]) + int(voiced[-1])) // 2
    save(
        "internal-pause",
        np.concatenate([normal[:half], np.zeros(19200, dtype=np.int16), normal[half:]]),
        kind="inserted_internal_pause",
    )
    save("late-packet", normal, kind="network_delay", late_packet=2, extra_delay_ms=800)
    for index, text in enumerate(INPUTS):
        samples = synth(text)
        (audio / f"input-{index}.wav").write_bytes(wav_bytes(samples))
        manifest["inputs"].append(
            {"id": f"input-{index}", "file": f"input-{index}.wav", "text": text}
        )
    (args.out / "fixtures.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)
    )
    (args.out / "weak_checks.json").write_text(json.dumps(checks, indent=2))
    print(json.dumps(checks, indent=2), flush=True)


if __name__ == "__main__":
    main()
