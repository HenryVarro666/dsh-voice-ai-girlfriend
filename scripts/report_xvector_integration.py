#!/usr/bin/env python3
"""Publish numeric traces; keep all audio and verbatim transcripts private."""

import argparse
import hashlib
import json
import statistics
import wave
from pathlib import Path

import numpy as np
from integration_metrics import cancel_metrics, e2e_metrics, queue_metrics


def scrub(value):
    if isinstance(value, list):
        return [scrub(x) for x in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {"text", "transcript", "heard"} and isinstance(item, str):
                result[key + "_sha256"] = hashlib.sha256(item.encode()).hexdigest()
                result[key + "_chars"] = len(item)
            else:
                result[key] = scrub(item)
        return result
    return value


def load(path):
    return json.loads(path.read_text())


def med(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return statistics.median(values) if values else None


def summarize(rows, metric_fn):
    metrics = [metric_fn(row) for row in rows]
    good = [r for r in metrics if "error" not in r]
    numeric = {
        key
        for r in good
        for key, value in r.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    return {
        "n": len(rows),
        "errors": len(rows) - len(good),
        "medians": {key: med(good, key) for key in sorted(numeric)},
        "range_voice_to_voice_ms": [
            min(r["voice_to_voice_ms"] for r in good),
            max(r["voice_to_voice_ms"] for r in good),
        ]
        if good and "voice_to_voice_ms" in good[0]
        else None,
    }


def import_private(player, e2e, out):
    bounds = {}
    rows = load(player / "browser-player.json")
    for row in rows:
        for server in row["server"]:
            path = player / "streams" / f"{server['stream_id']}.wav"
            with wave.open(str(path)) as wav:
                samples = np.frombuffer(
                    wav.readframes(wav.getnframes()), dtype="<i2"
                ).astype(np.int32)
            voiced = np.flatnonzero(np.abs(samples) >= 328)
            bounds[server["stream_id"]] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "samples": len(samples),
                "first_voiced_sample": int(voiced[0]),
                "last_voiced_sample": int(voiced[-1]),
            }
    for name, value in {
        "queue": rows,
        "cancel": load(player / "browser-interruption.json"),
        "e2e": load(
            e2e
            / (
                "browser-e2e-combined.json"
                if (e2e / "browser-e2e-combined.json").exists()
                else "browser-e2e.json"
            )
        ),
        "queue-audio-bounds": bounds,
    }.items():
        (out / f"{name}.json").write_text(
            json.dumps(scrub(value), ensure_ascii=False, indent=2) + "\n"
        )


def main(profiles=("baseline", "xvector"), title=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True, type=Path)
    parser.add_argument("--player", type=Path)
    parser.add_argument("--e2e", type=Path)
    args = parser.parse_args()
    data = args.folder / "data"
    if bool(args.player) != bool(args.e2e):
        parser.error(
            "Supply both private input folders, or neither for public recomputation"
        )
    if args.player:
        import_private(args.player, args.e2e, data)
    queue, cancel, e2e = [
        load(data / f"{key}.json") for key in ("queue", "cancel", "e2e")
    ]
    bounds = load(data / "queue-audio-bounds.json")
    result = {
        "cancel": {},
        "queue": {},
        "e2e": {},
        "first_requests": [],
        "same_first_clause_pairs": [],
    }
    for profile in profiles:
        result["cancel"][profile], result["queue"][profile], result["e2e"][profile] = (
            {},
            {},
            {},
        )
        for kind in ("before", "after", "queued"):
            rows = [
                r
                for r in cancel
                if r.get("policy") == profile
                and r.get("key", "").startswith(f"cancel-{kind}-")
            ]
            result["cancel"][profile][kind] = summarize(rows, cancel_metrics)
        for kind in ("tail", "natural"):
            rows = [
                r
                for r in queue
                if r.get("policy") == profile
                and r.get("key", "").startswith(f"queue-{kind}-")
            ]
            result["queue"][profile][kind] = summarize(
                rows, lambda r: queue_metrics(r, bounds)
            )
        warm = [
            r
            for r in e2e
            if r.get("policy", r.get("partial", {}).get("policy")) == profile
            and not r.get("warmup", r.get("partial", {}).get("warmup", False))
        ]
        for path in ("fast", "fallback", "all"):
            group = [
                r for r in warm if path == "all" or e2e_metrics(r).get("path") == path
            ]
            result["e2e"][profile][path] = summarize(group, e2e_metrics)
    for row in e2e:
        if row.get("warmup"):
            result["first_requests"].append(
                {"profile": row["policy"], "metrics": e2e_metrics(row)}
            )
    arms = profiles
    groups = {}
    for row in e2e:
        if not row.get("warmup") and "error" not in row:
            groups.setdefault(row["key"], {})[row["policy"]] = e2e_metrics(row)
    for key, pair in groups.items():
        if (
            set(pair) == set(arms)
            and pair["baseline"]["first_clause_sha256"]
            == pair[arms[1]]["first_clause_sha256"]
        ):
            result["same_first_clause_pairs"].append({"key": key, "profiles": pair})
    result["counts"] = {
        "cancel": len(cancel),
        "queue": len(queue),
        "e2e": len(e2e),
        "e2e_failed_records": sum("error" in r for r in e2e),
        "e2e_samples_equal": sum(
            "error" not in e2e_metrics(r)
            and e2e_metrics(r).get("received_samples")
            == e2e_metrics(r).get("scheduled_samples")
            for r in e2e
            if "error" not in r
        ),
        "cancel_late_sources": sum(
            r.get("late_sources_in_stopped_segment", 0) for r in cancel
        ),
    }
    (data / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    figure(args.folder, result, e2e, arms, title)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def figure(folder, result, rows, arms=("baseline", "xvector"), title=None):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {arms[0]: "#59779c", arms[1]: "#d37843"}
    fig, axes = plt.subplots(2, 2, figsize=(11, 7.3), layout="constrained")
    ax = axes[0, 0]
    for j, profile in enumerate(colors):
        xs = [i + (-0.17 if j == 0 else 0.17) for i in range(3)]
        ys = [
            result["cancel"][profile][kind]["medians"][
                "consumer_close_to_producer_exit_ms"
            ]
            for kind in ("before", "after", "queued")
        ]
        ax.bar(xs, ys, width=0.32, color=colors[profile], label=profile)
    ax.set(
        xticks=[0, 1, 2],
        xticklabels=["Before PCM", "After audio", "Queued"],
        ylabel="Milliseconds",
        title=(
            "Cancel: close to generation / skipped waiter exit"
            if "fixed" in arms
            else "Cancellation: consumer close to producer exit"
        ),
    )
    ax.legend(frameon=False)
    ax = axes[0, 1]
    for j, profile in enumerate(colors):
        xs = [i + (-0.17 if j == 0 else 0.17) for i in range(2)]
        ys = [
            result["queue"][profile][kind]["medians"]["playback_total_ms"] / 1000
            for kind in ("tail", "natural")
        ]
        ax.bar(xs, ys, width=0.32, color=colors[profile])
    ax.set(
        xticks=[0, 1],
        xticklabels=["Known model tail", "Natural short sentences"],
        ylabel="Seconds",
        title="Two-clip playback to actual ended; no crop",
    )
    ax = axes[1, 0]
    for i, path in enumerate(("fast", "fallback")):
        for j, profile in enumerate(colors):
            x = i * 3 + j
            values = [
                e2e_metrics(r)["voice_to_voice_ms"] / 1000
                for r in rows
                if not r.get("warmup")
                and "error" not in r
                and r["policy"] == profile
                and e2e_metrics(r)["path"] == path
            ]
            ax.scatter([x] * len(values), values, c=colors[profile], s=26)
            if values:
                ax.plot(
                    [x - 0.15, x + 0.15],
                    [statistics.median(values)] * 2,
                    color="black",
                    lw=2,
                )
            ax.text(
                x,
                0.05,
                f"n={len(values)}",
                ha="center",
                transform=ax.get_xaxis_transform(),
            )
    ax.set(
        xticks=[0, 1, 3, 4],
        xticklabels=[f"{a}\n{p}" for p in ("fast", "fallback") for a in arms],
        ylabel="Speech end to first voiced output (s)",
        title="Real DSH; synthetic input; warm turns only",
    )
    ax = axes[1, 1]
    labels, before, tts = [], [], []
    for path in ("fast", "fallback"):
        for profile in colors:
            m = result["e2e"][profile][path]["medians"]
            labels.append(f"{profile} / {path}")
            before.append(m.get("before_tts_ms", 0) / 1000)
            tts.append(m.get("tts_to_voice_ms", 0) / 1000)
    ax.barh(labels, before, color="#b0bdca", label="Before TTS request")
    ax.barh(
        labels, tts, left=before, color="#647c96", label="TTS request to voiced output"
    )
    ax.set(xlabel="Seconds", title="Stage medians (sum need not equal total median)")
    ax.legend(frameon=False, fontsize=8)
    for ax in axes.flat:
        ax.grid(axis="y", alpha=0.16)
    fig.suptitle(
        title or "apex ICL / vector integration | production unchanged | no tail crop",
        fontsize=13,
    )
    for ext in ("svg", "png"):
        fig.savefig(folder / f"integration.{ext}", dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    main()
