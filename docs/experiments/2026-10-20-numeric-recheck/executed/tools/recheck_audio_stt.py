#!/usr/bin/env python3
"""Replay immutable archived PCM sequentially; keep every ASR result private."""

import argparse
import hashlib
import io
import json
import random
import time
import wave
from pathlib import Path

from bench_generation_end import asr, edit_distance, normalize
from bench_generation_layout import compute_pids
from number_reading import check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    suite = json.loads(args.suite.read_text())
    args.out.mkdir(parents=True, exist_ok=False)
    allowed = compute_pids()
    cache, jobs = {}, []
    for item in suite:
        data = Path(item["wav"]).expanduser().read_bytes()
        hashed = hashlib.sha256(data).hexdigest()
        if item.get("sha256", hashed) != hashed:
            raise ValueError(f"Archived WAV changed: {item['id']}")
        with wave.open(io.BytesIO(data)) as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (
                1,
                2,
                16000,
            ):
                raise ValueError("Expected original mono PCM16/16kHz WAV")
            cache[item["id"]] = (wav.readframes(wav.getnframes()), hashed)
        jobs.extend((item, repeat) for repeat in range(item["repeats"]))
    random.Random(20261025).shuffle(jobs)
    (args.out / "manifest.json").write_text(
        json.dumps(
            {
                "suite": suite,
                "suite_sha256": hashlib.sha256(args.suite.read_bytes()).hexdigest(),
                "allowed_gpu_pids": sorted(allowed),
                "planned_requests": len(jobs),
                "script_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    for index, (item, repeat) in enumerate(jobs):
        unexpected = compute_pids() - allowed if index % 10 == 0 else set()
        if unexpected:
            raise RuntimeError(
                f"New GPU job; replay stopped without unloading: {unexpected}"
            )
        pcm, hashed = cache[item["id"]]
        row = {"id": item["id"], "repeat": repeat, "wav_sha256": hashed}
        started = time.perf_counter()
        try:
            transcript = asr(pcm)
            row.update(
                asr_ms=(time.perf_counter() - started) * 1000,
                normalized_asr_sha256=hashlib.sha256(
                    normalize(transcript).encode()
                ).hexdigest(),
                exact=normalize(transcript) == normalize(item["text"]),
                edits=edit_distance(normalize(transcript), normalize(item["text"])),
            )
            if "case" in item:
                parsed = check(item["case"], transcript)
                row["fields"] = parsed["fields"]
                row["field_status"] = parsed["status"]
                row["observed_code"] = parsed["code"]
            with (args.out / "transcripts.jsonl").open("a") as f:
                f.write(
                    json.dumps(row | {"transcript": transcript}, ensure_ascii=False)
                    + "\n"
                )
        except Exception as exc:  # noqa: BLE001 - retain failed ASR requests in denominator
            row["error"] = f"{type(exc).__name__}: {exc}"
        with (args.out / "repeats.jsonl").open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if index % 10 == 0 or index == len(jobs) - 1:
            print(
                "STT_REPLAY",
                index + 1,
                "/",
                len(jobs),
                item["id"],
                row.get("field_status", row.get("exact")),
                row.get("error", ""),
                flush=True,
            )
    unexpected = compute_pids() - allowed
    (args.out / "complete.json").write_text(
        json.dumps(
            {"requests": len(jobs), "unexpected_gpu_pids_after": sorted(unexpected)}
        )
    )


if __name__ == "__main__":
    main()
