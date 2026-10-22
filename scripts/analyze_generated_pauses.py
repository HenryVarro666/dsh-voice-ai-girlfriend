#!/usr/bin/env python3
"""Inspect generated PCM low-amplitude runs; no human silence labels implied."""

import argparse
import json
import wave
from pathlib import Path

import numpy as np


def describe(samples):
    above = np.flatnonzero(np.abs(samples.astype(np.int32)) >= 328)
    if not len(above):
        return {"all_low_amplitude": True, "audio_ms": len(samples) / 16}
    low = np.abs(samples.astype(np.int32)) < 328
    edges = np.flatnonzero(np.diff(np.r_[False, low, False].astype(np.int8)))
    lengths = edges[1::2] - edges[::2]
    return {
        "audio_ms": len(samples) / 16,
        "leading_low_ms": int(above[0]) / 16,
        "trailing_low_ms": (len(samples) - int(above[-1]) - 1) / 16,
        "longest_low_run_ms": int(lengths.max(initial=0)) / 16,
        "low_fraction": float(low.mean()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    args = parser.parse_args()
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
        result = {
            k: row[k]
            for k in ["trial_id", "seed", "phase", "policy", "text_index"]
            if k in row
        }
        result["whole"] = describe(samples)
        if "segments" in row:
            start, segments = 0, []
            for segment in row["segments"]:
                count = round(segment["audio_s"] * 16000)
                segments.append(
                    {
                        "text": segment["text"],
                        **describe(samples[start : start + count]),
                    }
                )
                start += count
            assert start == len(samples)
            result["segments"] = segments
        results.append(result)
    (args.folder / "pause_analysis.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2)
    )
    for result in results:
        if result["whole"].get("longest_low_run_ms", 0) > 1000:
            print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
