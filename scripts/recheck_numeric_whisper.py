#!/usr/bin/env python3
"""Check seven frozen PCM files with an independent CPU ASR, without hints."""

import argparse
import hashlib
import json
import time
import wave
from pathlib import Path

import numpy as np
from number_reading import check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    import torch
    from huggingface_hub import HfApi, snapshot_download
    from transformers import WhisperForConditionalGeneration, WhisperProcessor

    torch.set_num_threads(4)
    model_id = "openai/whisper-small"
    revision = HfApi().model_info(model_id).sha
    manifest = {
        "model": model_id,
        "revision": revision,
        "device": "cpu",
        "dtype": "float32",
        "language": "zh",
        "task": "transcribe",
        "decoders": [1, 5],
        "hints": None,
        "suite_sha256": hashlib.sha256(args.suite.read_bytes()).hexdigest(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    folder = Path(
        snapshot_download(
            model_id,
            revision=revision,
            cache_dir=args.out.parent / "whisper-cache",
            allow_patterns=["*.json", "*.txt", "*.safetensors"],
        )
    )
    manifest["files"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in folder.iterdir()
        if p.is_file()
    }
    processor = WhisperProcessor.from_pretrained(folder, local_files_only=True)
    model = WhisperForConditionalGeneration.from_pretrained(
        folder, local_files_only=True, dtype=torch.float32
    ).eval()
    for item in json.loads(args.suite.read_text()):
        path = Path(item["wav"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
        with wave.open(str(path)) as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
                16000,
                1,
                2,
            )
            audio = (
                np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(
                    np.float32
                )
                / 32768
            )
        inputs = processor(audio, sampling_rate=16000, return_tensors="pt")
        for beams in (1, 5):
            started = time.perf_counter()
            with torch.inference_mode():
                tokens = model.generate(
                    **inputs,
                    language="zh",
                    task="transcribe",
                    do_sample=False,
                    num_beams=beams,
                    max_new_tokens=96,
                    return_timestamps=False,
                )
            transcript = processor.batch_decode(tokens, skip_special_tokens=True)[0]
            canonical = transcript
            for src, dst in (
                ("編號", "编号"),
                ("時間", "时间"),
                ("時", "时"),
                ("點", "点"),
                ("兩", "两"),
            ):
                canonical = canonical.replace(src, dst)
            row = {
                "id": item["id"],
                "wav_sha256": item["sha256"],
                "beams": beams,
                "asr_ms": (time.perf_counter() - started) * 1000,
                "transcript_sha256": hashlib.sha256(transcript.encode()).hexdigest(),
                "strict": check(item["case"], transcript),
                "label_canonicalized": check(item["case"], canonical),
            }
            with (args.out / "transcripts.jsonl").open("a") as out:
                out.write(
                    json.dumps(row | {"transcript": transcript}, ensure_ascii=False)
                    + "\n"
                )
            with (args.out / "checks.jsonl").open("a") as out:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                "WHISPER_CHECK",
                item["id"],
                beams,
                row["label_canonicalized"]["status"],
                row["label_canonicalized"]["code"],
                flush=True,
            )
    manifest["completed"] = True
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
