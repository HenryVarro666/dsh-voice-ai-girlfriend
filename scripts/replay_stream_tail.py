#!/usr/bin/env python3
"""Replay archived PCM chunks with original arrival times, without sleeping."""

import argparse
import hashlib
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np
from probe_tts_tail_crop import crop_count

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from stream_tail import TailGuard


def schedule(events, parts, cursor=None):
    first = None
    underrun = 0.0
    for event, part in zip(events, parts):
        at = event["at_ms"]
        if cursor is not None:
            underrun += max(0, at - cursor)
        start = max(at, cursor if cursor is not None else at)
        values = np.frombuffer(part, dtype="<i2").astype(np.int32)
        voice = np.flatnonzero(np.abs(values) >= 328)
        if first is None and len(voice):
            first = start + int(voice[0]) / 16
        cursor = start + len(values) / 16
    return {"first_voice_ms": first, "end_ms": cursor, "underrun_ms": underrun}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clause", required=True, type=Path)
    parser.add_argument("--tail", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--threshold", type=int, default=99)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if (args.out / "replay.json").exists():
        parser.error("output already exists")
    records = []
    for folder in [args.clause, args.tail]:
        rows = [
            json.loads(line)
            for line in (folder / "trials.jsonl").read_text().splitlines()
        ]
        for row in rows:
            name = (
                f"trial-{row['trial_id']:03d}.wav"
                if "trial_id" in row
                else f"{row['phase']}-{row['seed']:03d}.wav"
            )
            with wave.open(str(folder / "audio" / name), "rb") as wav:
                assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
                    16000,
                    1,
                    2,
                )
                audio = wav.readframes(wav.getnframes())
            segments = row.get("segments", [{"audio_s": len(audio) / 32000}])
            offset, cursor_before, cursor_after = 0, None, None
            first_before, first_after = None, None
            under_before, under_after = 0.0, 0.0
            details = []
            processing_ms = 0
            for segment in segments:
                count = round(segment["audio_s"] * 16000) * 2
                part = audio[offset : offset + count]
                offset += count
                known_times = "packets" in segment
                events = segment.get(
                    "packets", [{"at_ms": 0, "samples": len(part) // 2}]
                )
                pieces, pos = [], 0
                for event in events:
                    end = pos + event["samples"] * 2
                    pieces.append(part[pos:end])
                    pos = end
                assert pos == len(part)
                clock = [0]

                def source():
                    for event, piece in zip(events, pieces):
                        clock[0] = event["at_ms"]
                        yield piece
                    clock[0] = max(clock[0], segment.get("completed_ms", clock[0]))

                guard, outputs, times = TailGuard(args.threshold), [], []
                started = time.perf_counter()
                for output in guard.process(source()):
                    outputs.append(output)
                    times.append({"at_ms": clock[0], "samples": len(output) // 2})
                processing_ms += (time.perf_counter() - started) * 1000
                expected = (
                    crop_count(np.frombuffer(part, dtype="<i2"), args.threshold) * 2
                )
                assert b"".join(outputs) == part[:expected]
                detail = {
                    "removed_ms": guard.removed_samples / 16,
                    "peak_pending_bytes": guard.peak_pending_bytes,
                    "overflows": guard.overflows,
                    "offline_bytes_match": True,
                }
                if known_times:
                    before = schedule(events, pieces, cursor_before)
                    after = schedule(times, outputs, cursor_after)
                    first_before = (
                        first_before
                        if first_before is not None
                        else before["first_voice_ms"]
                    )
                    first_after = (
                        first_after
                        if first_after is not None
                        else after["first_voice_ms"]
                    )
                    cursor_before, cursor_after = before["end_ms"], after["end_ms"]
                    under_before += before["underrun_ms"]
                    under_after += after["underrun_ms"]
                    detail.update(before=before, after=after)
                details.append(detail)
            assert offset == len(audio)
            result = {
                "source": folder.name,
                "file": name,
                "segments": details,
                "processing_ms": processing_ms,
                "packet_times_available": known_times,
                "removed_ms": sum(d["removed_ms"] for d in details),
            }
            if known_times:
                result.update(
                    first_voice_delay_ms=first_after - first_before,
                    playback_saved_ms=cursor_before - cursor_after,
                    added_underrun_ms=under_after - under_before,
                )
            records.append(result)
    payload = {
        "scope": "Archived packet clock replay, not browser playback",
        "threshold": args.threshold,
        "guard_sha256": hashlib.sha256(
            Path(__file__)
            .resolve()
            .parents[1]
            .joinpath("bridge/stream_tail.py")
            .read_bytes()
        ).hexdigest(),
        "records": records,
    }
    (args.out / "replay.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2)
    )
    timed = [r for r in records if r["packet_times_available"]]
    print(
        json.dumps(
            {
                "records": len(records),
                "segments": sum(len(r["segments"]) for r in records),
                "timed": len(timed),
                "affected": [r for r in timed if r["removed_ms"]],
                "first_voice_delay_max_ms": max(
                    r["first_voice_delay_ms"] for r in timed
                ),
                "new_underruns": [r for r in timed if r["added_underrun_ms"] > 0.01],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
