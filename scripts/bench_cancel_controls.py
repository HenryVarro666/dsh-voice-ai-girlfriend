"""Replay four frozen old DSH clauses, no cancellation, with ICL in both arms."""

import argparse
import hashlib
import json
import time
from pathlib import Path

import httpx
import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("Refusing to overwrite existing evidence")
    source = json.loads(args.source.read_text())
    controls = []
    for index in range(4):
        row = next(
            r
            for r in source
            if r["key"] == f"case-{index}-r0" and r["policy"] == "baseline"
        )
        event = next(e for e in row["events"] if e["api"] == "tts/stream")
        controls.append(
            {"case": index, "text": event["text"], "seed": row["server"][0]["seed"]}
        )
    args.out.mkdir()
    (args.out / "frozen-controls.json").write_text(
        json.dumps(controls, ensure_ascii=False, indent=2)
    )
    results = []
    with httpx.Client(timeout=90) as client:
        for control in controls:
            for arm in (
                ("baseline", "fixed")
                if control["case"] % 2 == 0
                else ("fixed", "baseline")
            ):
                started, first, pcm = time.perf_counter(), None, b""
                with client.stream(
                    "POST",
                    f"http://127.0.0.1:8766/__voice_test__/tts/stream?policy={arm}",
                    json={"text": control["text"]},
                    headers={
                        "X-Experiment-Trial": f"control-{control['case']}",
                        "X-Experiment-Seed": str(control["seed"]),
                        "X-Experiment-Segment": "1",
                    },
                ) as response:
                    response.raise_for_status()
                    stream_id = response.headers["X-Test-Stream"]
                    for block in response.iter_bytes():
                        pcm += block
                        values = np.frombuffer(
                            pcm[: len(pcm) // 2 * 2], dtype="<i2"
                        ).astype(np.int32)
                        if first is None and np.any(np.abs(values) >= 328):
                            first = (time.perf_counter() - started) * 1000
                results.append(
                    {
                        "case": control["case"],
                        "profile": arm,
                        "stream_id": stream_id,
                        "request_to_voiced_pcm_ms": first,
                        "request_to_eof_ms": (time.perf_counter() - started) * 1000,
                        "pcm_samples": len(pcm) // 2,
                        "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                    }
                )
                (args.out / "controls.json").write_text(json.dumps(results, indent=2))
                print(results[-1], flush=True)


if __name__ == "__main__":
    main()
