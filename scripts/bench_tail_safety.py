#!/usr/bin/env python3
"""Independent tail-safety comparison on archived WAVs and owned HTTP streams."""

import argparse
import asyncio
import hashlib
import json
import sys
import time
import wave
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from stream_tail import TailGuard
from stream_tail_safe import AsyncTailStream, ExactZeroTailGuard

TEXTS = [
    "你好，我在听。",
    "今天先休息吧。",
    "你说的是星期四吗？",
    "慢慢来，别着急呀。",
    "先把文件保存好，然后再关闭窗口。",
    "好的，我会等你把话说完。",
]


def pcm(value, count):
    return np.full(count, value, dtype="<i2").tobytes()


def read_wav(path):
    with wave.open(str(path), "rb") as source:
        assert (
            source.getframerate(),
            source.getnchannels(),
            source.getsampwidth(),
        ) == (16000, 1, 2)
        return source.readframes(source.getnframes())


def write_wav(path, audio):
    with wave.open(str(path), "wb") as dest:
        dest.setnchannels(1)
        dest.setsampwidth(2)
        dest.setframerate(16000)
        dest.writeframes(audio)


def schedule(events):
    cursor, first, gaps = None, None, 0
    for at, data in events:
        if not data:
            continue
        start = max(at, cursor if cursor is not None else at)
        if cursor is not None:
            gaps += max(0, at - cursor)
        samples = np.frombuffer(data, dtype="<i2").astype(np.int32)
        voice = np.flatnonzero(np.abs(samples) >= 328)
        if first is None and len(voice):
            first = start + int(voice[0]) / 16
        cursor = start + len(data) / 32
    return {"first_voice_ms": first, "ideal_end_ms": cursor, "underrun_ms": gaps}


def compare(audio):
    result = {}
    for label, guard in (
        ("amplitude_v2", TailGuard()),
        ("exact_zero", ExactZeroTailGuard()),
    ):
        output = b"".join(
            guard.process(audio[i : i + 1024] for i in range(0, len(audio), 1024))
        )
        assert audio.startswith(output)
        deleted = np.frombuffer(audio[len(output) :], dtype="<i2")
        result[label] = {
            "removed_ms": (len(audio) - len(output)) / 32,
            "nonzero_samples_removed": int(np.count_nonzero(deleted)),
            "peak_pending_bytes": guard.peak_pending_bytes,
            "overflows": guard.overflows,
        }
    return result


def archive(root):
    cases, first_audio = [], None
    folders = [
        (root / "20261020-first-clause/main-01", "clause"),
        (root / "20261020-tts-tail/main-01", "tail"),
        *(
            (root / "20261020-stream-guard" / name, "model")
            for name in ("0.6b", "1.7b", "holdout-0.6b", "holdout-1.7b")
        ),
    ]
    for folder, kind in folders:
        for line in (folder / "trials.jsonl").read_text().splitlines():
            row = json.loads(line)
            if kind == "clause":
                name = f"trial-{row['trial_id']:03d}.wav"
            elif kind == "tail":
                name = f"{row['phase']}-{row['seed']:03d}.wav"
            else:
                name = f"text-{row['text_index']}-seed-{row['seed']:02d}-original.wav"
            audio = read_wav(folder / "audio" / name)
            if first_audio is None and kind == "model" and folder.name == "0.6b":
                first_audio = audio
            offset, segments = 0, []
            for segment in row.get("segments", [{"audio_s": len(audio) / 32000}]):
                end = offset + round(segment["audio_s"] * 16000) * 2
                part = audio[offset:end]
                segments.append(compare(part))
                offset = end
            assert offset == len(audio)
            cases.append(
                {
                    "source": str(folder.relative_to(root)),
                    "file": name,
                    "sha256": hashlib.sha256(audio).hexdigest(),
                    "segments": segments,
                }
            )
    return cases, first_audio


def weak_challenges(audio, out):
    values = np.frombuffer(audio, dtype="<i2").astype(np.int32)
    active = np.flatnonzero(np.abs(values) >= 328)
    end = int(active[-1]) + 1
    start = max(0, end - 8000)
    tail = values[start:end]
    rows = []
    for peak in (70, 1):
        scaled = np.rint(tail * peak / np.abs(tail).max()).astype("<i2")
        # Include a >1s deliberately weak continuation as an adversarial case.
        weak = np.tile(scaled, 3).tobytes()
        prefix = values[:start].astype("<i2").tobytes()
        data = prefix + weak + pcm(0, 32000)
        result = compare(data)
        write_wav(out / f"weak-peak-{peak}-input.wav", data)
        for label, guard in (
            ("amplitude_v2", TailGuard()),
            ("exact_zero", ExactZeroTailGuard()),
        ):
            output = b"".join(guard.process([data]))
            write_wav(out / f"weak-peak-{peak}-{label}.wav", output)
        rows.append(
            {
                "peak": peak,
                "kind": "attenuated repeated generated ending; not natural soft speech",
                **result,
            }
        )
    return rows


async def paced_fixtures():
    fixtures = [
        ("fast_zero_tail", [(0, pcm(1000, 1600) + pcm(0, 32000))]),
        (
            "slow_internal_pause",
            [(0, pcm(1000, 1600) + pcm(0, 9600)), (200, pcm(70, 1600))],
        ),
        (
            "low_playable_lead",
            [(0, pcm(1000, 160)), (300, pcm(0, 8000)), (600, pcm(70, 1600))],
        ),
        ("weak_tail", [(0, pcm(1000, 100) + pcm(1, 24000) + pcm(0, 32000))]),
    ]
    rows = []
    for name, packets in fixtures:
        before, after = [], []
        t0 = time.perf_counter()

        async def source():
            for at, data in packets:
                await asyncio.sleep(max(0, at / 1000 - (time.perf_counter() - t0)))
                before.append(((time.perf_counter() - t0) * 1000, data))
                yield data

        stream = AsyncTailStream()
        async for data in stream.process(source()):
            after.append(((time.perf_counter() - t0) * 1000, data))
        original, output = b"".join(p[1] for p in before), b"".join(p[1] for p in after)
        b, a = schedule(before), schedule(after)
        rows.append(
            {
                "name": name,
                "before": b,
                "after": a,
                "nonzero_removed": int(
                    np.count_nonzero(
                        np.frombuffer(original[len(output) :], dtype="<i2")
                    )
                ),
                "unchanged": output == original,
                "timed_releases": stream.timed_releases,
                "added_underrun_ms": a["underrun_ms"] - b["underrun_ms"],
            }
        )
    return rows


async def http_trials(out):
    rows = []
    async with httpx.AsyncClient(timeout=120) as client:
        for index, text in enumerate(TEXTS):
            before, after = [], []
            stream = AsyncTailStream()
            t0 = time.perf_counter()
            async with client.stream(
                "POST", "http://127.0.0.1:8765/api/tts/stream", json={"text": text}
            ) as response:
                response.raise_for_status()

                async def source():
                    carry = b""
                    async for packet in response.aiter_bytes():
                        packet = carry + packet
                        size = len(packet) - len(packet) % 2
                        carry = packet[size:]
                        data = packet[:size]
                        before.append(((time.perf_counter() - t0) * 1000, data))
                        yield data
                    if carry:
                        raise ValueError("unaligned upstream EOF")

                async for data in stream.process(source()):
                    after.append(((time.perf_counter() - t0) * 1000, data))
            audio, guarded = (
                b"".join(p[1] for p in before),
                b"".join(p[1] for p in after),
            )
            assert audio.startswith(guarded)
            write_wav(out / f"live-{index}-original.wav", audio)
            write_wav(out / f"live-{index}-guarded.wav", guarded)
            b, a = schedule(before), schedule(after)
            rows.append(
                {
                    "index": index,
                    "text": text,
                    "before": b,
                    "after": a,
                    "removed_ms": (len(audio) - len(guarded)) / 32,
                    "nonzero_removed": int(
                        np.count_nonzero(
                            np.frombuffer(audio[len(guarded) :], dtype="<i2")
                        )
                    ),
                    "processing_wall_ms": (time.perf_counter() - t0) * 1000,
                    "first_voice_delay_ms": a["first_voice_ms"] - b["first_voice_ms"],
                    "added_underrun_ms": a["underrun_ms"] - b["underrun_ms"],
                    "peak_pending_bytes": stream.guard.peak_pending_bytes,
                }
            )
    return rows


async def cancel_trial(delay):
    first = asyncio.Event()
    closed = asyncio.Event()
    stream = AsyncTailStream()
    output = []
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=120) as client:

        async def source():
            try:
                async with client.stream(
                    "POST",
                    "http://127.0.0.1:8765/api/tts/stream",
                    json={"text": "这是独立取消实验，请慢慢把下面的话说完。" * 8},
                ) as response:
                    response.raise_for_status()
                    async for data in response.aiter_bytes():
                        first.set()
                        yield data
            finally:
                closed.set()

        async def consume():
            async for data in stream.process(source()):
                output.append((time.perf_counter() - t0, len(data)))

        task = asyncio.create_task(consume())
        await asyncio.wait_for(first.wait(), 120)
        await asyncio.sleep(delay)
        count = len(output)
        start = time.perf_counter()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return {
            "delay_after_first_packet_ms": delay * 1000,
            "cancel_to_close_ms": (time.perf_counter() - start) * 1000,
            "source_closed": closed.is_set(),
            "outputs_after_cancel": len(output) - count,
            "pending_bytes": stream.guard.pending_bytes,
        }


async def run(args):
    args.out.mkdir(parents=True, exist_ok=False)
    audio_out = args.out / "audio"
    audio_out.mkdir()
    rows, first_audio = archive(args.archive)
    report = {
        "scope": "Exact-zero independent client; no production deployment or real microphone",
        "archived": rows,
        "weak_challenges": weak_challenges(first_audio, audio_out),
        "paced": await paced_fixtures(),
        "live": await http_trials(audio_out),
        "cancel": [],
    }
    # Space cancelled requests so teardown from one cannot contaminate the next.
    for delay in (0.05, 0.15, 0.35):
        report["cancel"].append(await cancel_trial(delay))
        await asyncio.sleep(1)
    report["hashes"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (
            Path(__file__),
            Path(__file__).resolve().parents[1] / "bridge/stream_tail.py",
            Path(__file__).resolve().parents[1] / "bridge/stream_tail_safe.py",
        )
    }
    (args.out / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(
        json.dumps(
            {
                "archived": len(rows),
                "live": report["live"],
                "cancel": report["cancel"],
                "paced": report["paced"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
