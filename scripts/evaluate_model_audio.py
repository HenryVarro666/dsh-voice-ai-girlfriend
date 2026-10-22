#!/usr/bin/env python3
"""ASR checks and v2 packet-clock replay for completed isolated model runs."""

import argparse
import hashlib
import json
import re
import sys
import urllib.request
import wave
from pathlib import Path

from replay_stream_tail import schedule

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from stream_tail import TailGuard


def normalize(text):
    return re.sub(r"[\W_]", "", text).lower()


def edit_distance(a, b):
    previous = list(range(len(b) + 1))
    for i, first in enumerate(a, 1):
        current = [i]
        for j, second in enumerate(b, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[j] + 1,
                    previous[j - 1] + (first != second),
                )
            )
        previous = current
    return previous[-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    for name in ["0.6b", "1.7b", "holdout-0.6b", "holdout-1.7b"]:
        folder = args.root / name
        manifest = json.loads((folder / "manifest.json").read_text())
        if not manifest.get("completed"):
            raise RuntimeError(f"Incomplete run: {name}")
        if (folder / "asr_check.json").exists():
            raise RuntimeError(f"Checks already exist: {name}")
        rows = [
            json.loads(line)
            for line in (folder / "trials.jsonl").read_text().splitlines()
        ]
        checks, replays = [], []
        (folder / "audio-v2").mkdir(exist_ok=True)
        for row in rows:
            key = f"text-{row['text_index']}-seed-{row['seed']:02d}"
            with wave.open(str(folder / "audio" / f"{key}-original.wav"), "rb") as wav:
                assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
                    16000,
                    1,
                    2,
                )
                raw = wav.readframes(wav.getnframes())
            clock, pieces = [0], []
            position = 0
            for event in row["original"]["packets"]:
                end = position + event["samples"] * 2
                pieces.append(raw[position:end])
                position = end
            assert position == len(raw)

            def source():
                for event, part in zip(row["original"]["packets"], pieces):
                    clock[0] = event["at_ms"]
                    yield part
                clock[0] = row["total_ms"]

            guard, outputs, events = TailGuard(), [], []
            for part in guard.process(source()):
                outputs.append(part)
                events.append({"at_ms": clock[0], "samples": len(part) // 2})
            cropped = b"".join(outputs)
            assert cropped == raw[: len(cropped)]
            before = schedule(row["original"]["packets"], pieces)
            after = schedule(events, outputs)
            replays.append(
                {
                    "text_index": row["text_index"],
                    "seed": row["seed"],
                    "removed_ms": guard.removed_samples / 16,
                    "peak_pending_bytes": guard.peak_pending_bytes,
                    "overflows": guard.overflows,
                    "first_voice_delay_ms": after["first_voice_ms"]
                    - before["first_voice_ms"],
                    "playback_saved_ms": before["end_ms"] - after["end_ms"],
                    "added_underrun_ms": after["underrun_ms"] - before["underrun_ms"],
                }
            )
            variants = [("original", raw)]
            if cropped != raw:
                variants.append(("guard-v2", cropped))
                with wave.open(str(folder / "audio-v2" / f"{key}.wav"), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(16000)
                    wav.writeframes(cropped)
            expected = normalize(manifest["texts"][row["text_index"]])
            for kind, data in variants:
                request = urllib.request.Request(
                    "http://127.0.0.1:8765/api/stt",
                    data=data,
                    headers={"Content-Type": "application/octet-stream"},
                )
                with urllib.request.urlopen(request, timeout=90) as response:
                    result = json.load(response)
                recognized = normalize(result["text"])
                checks.append(
                    {
                        "text_index": row["text_index"],
                        "seed": row["seed"],
                        "kind": kind,
                        "matched": expected == recognized,
                        "edit_distance": edit_distance(expected, recognized),
                        "expected_chars": len(expected),
                        "recognized_chars": len(recognized),
                        "recognized_sha256": hashlib.sha256(
                            recognized.encode()
                        ).hexdigest(),
                    }
                )
                if expected != recognized:
                    print("MISMATCH", name, key, kind, expected, recognized, flush=True)
        (folder / "asr_check.json").write_text(
            json.dumps(checks, ensure_ascii=False, indent=2)
        )
        (folder / "guard_v2.json").write_text(
            json.dumps(
                {
                    "guard_sha256": hashlib.sha256(
                        Path(__file__)
                        .resolve()
                        .parents[1]
                        .joinpath("bridge/stream_tail.py")
                        .read_bytes()
                    ).hexdigest(),
                    "records": replays,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print(
            "CHECKED",
            name,
            len(checks),
            "matched",
            sum(r["matched"] for r in checks),
            flush=True,
        )


if __name__ == "__main__":
    main()
