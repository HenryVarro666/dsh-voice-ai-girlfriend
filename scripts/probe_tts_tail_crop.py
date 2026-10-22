#!/usr/bin/env python3
"""Offline conservative end-only crop on archived generated audio."""

import argparse
import json
import wave
from pathlib import Path

import numpy as np


def crop_count(samples, threshold=99):
    indices = np.flatnonzero(np.abs(samples.astype(np.int32)) >= threshold)
    if not len(indices):
        return len(samples)
    quiet = len(samples) - int(indices[-1]) - 1
    if quiet <= 16000:
        return len(samples)
    return min(len(samples), int(indices[-1]) + 1 + 4800)


def write_wav(path, samples):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(samples.astype("<i2").tobytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    args = parser.parse_args()
    out = args.folder / "cropped-audio"
    out.mkdir(exist_ok=True)
    rows = [
        json.loads(line)
        for line in (args.folder / "trials.jsonl").read_text().splitlines()
    ]
    results = []
    for row in rows:
        name = (
            f"trial-{row['trial_id']:03d}.wav"
            if "trial_id" in row
            else f"{row['phase']}-{row['seed']:03d}.wav"
        )
        with wave.open(str(args.folder / "audio" / name), "rb") as source:
            assert source.getframerate() == 16000
            samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
        segments = row.get("segments", [{"audio_s": len(samples) / 16000}])
        start, trimmed, counts = 0, [], []
        for segment in segments:
            count = round(segment["audio_s"] * 16000)
            part = samples[start : start + count]
            keep = crop_count(part)
            assert (
                keep == len(part)
                or np.abs(part[keep:].astype(np.int32)).max(initial=0) < 99
            )
            trimmed.append(part[:keep])
            counts.append(keep)
            start += count
        assert start == len(samples)
        result = {
            k: row[k]
            for k in ["trial_id", "seed", "phase", "policy", "text_index"]
            if k in row
        }
        result.update(
            removed_ms=(len(samples) - sum(counts)) / 16,
            original_audio_ms=len(samples) / 16,
            cropped_audio_ms=sum(counts) / 16,
            prefix_samples_preserved=True,
        )
        if "segments" in row:
            cursor = None
            for segment, keep in zip(segments, counts):
                remaining = keep
                for packet in segment["packets"]:
                    take = min(remaining, packet["samples"])
                    if take:
                        cursor = (
                            max(cursor or packet["at_ms"], packet["at_ms"]) + take / 16
                        )
                        remaining -= take
                assert remaining == 0
            result["original_ideal_end_ms"] = row["ideal_end_ms"]
            result["cropped_ideal_end_ms"] = cursor
            result["estimated_playback_saved_ms"] = row["ideal_end_ms"] - cursor
        if result["removed_ms"]:
            write_wav(out / name, np.concatenate(trimmed))
        results.append(result)
    (args.folder / "crop_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2)
    )
    print(
        json.dumps(
            [r for r in results if r["removed_ms"]], ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
