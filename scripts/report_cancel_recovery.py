"""Import private recovery evidence, or recompute the numeric public report."""

import argparse
import ast
import hashlib
import json
import statistics
import wave
from pathlib import Path

from report_xvector_integration import scrub


def load(path):
    return json.loads(path.read_text())


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def import_evidence(private, folder):
    data = folder / "data"
    streams = load(private / "run-1/server-streams.json")
    restart = load(private / "restart-1/results.json")
    api = load(private / "api-1/results.json")
    browser = load(private / "run-1/browser-interruption.json")
    index = {s["stream_id"]: s for s in streams}
    assert (
        len(streams) == 79
        and len(api) == 36
        and len(browser) == 4
        and len(restart) == 2
    )
    # Use the final server trace; the original per-case snapshots remain private.
    for row in api + browser:
        row["server"] = [index[s["stream_id"]] for s in row["server"]]
    checked = 0
    server_runs = {}
    for run in ("run-1", "run-2"):
        server_runs[run] = load(private / run / "server-streams.json")
        for server in server_runs[run]:
            path = private / run / "streams" / f"{server['stream_id']}.wav"
            with wave.open(str(path)) as wav:
                assert wav.getparams()[:3] == (1, 2, 16000)
                assert wav.getnframes() == server.get("sent_samples", 0)
                pcm = wav.readframes(wav.getnframes())
                assert len(pcm) == wav.getnframes() * 2
            server["pcm_sha256"] = hashlib.sha256(pcm).hexdigest()
            if run == "run-1":
                index[server["stream_id"]]["pcm_sha256"] = server["pcm_sha256"]
            checked += 1
    checked_clients = 0

    def check_client(result, client_dir, run):
        nonlocal checked_clients
        with wave.open(str(client_dir / f"client-{result['stream_id']}.wav")) as wav:
            assert wav.getparams()[:3] == (1, 2, 16000)
            pcm = wav.readframes(wav.getnframes())
            assert wav.getnframes() == result["received_samples"]
            assert len(pcm) == wav.getnframes() * 2
            assert hashlib.sha256(pcm).hexdigest() == result["pcm_sha256"]
        with wave.open(
            str(private / run / "streams" / f"{result['stream_id']}.wav")
        ) as wav:
            offered = wav.readframes(wav.getnframes())
        assert offered.startswith(pcm), result["stream_id"]
        if not result["read_error"]:
            assert pcm == offered
        checked_clients += 1

    for row in api:
        outputs = [row[k] for k in ("result", "followup") if k in row]
        if row["kind"].startswith("fault-"):
            outputs.append(row["target"])
        for result in outputs:
            check_client(result, private / "api-1", "run-1")
    for row in restart:
        check_client(row["result"], private / "restart-1", "run-2")
    shutdown = load(private / "shutdown-1/shutdown.json")
    check_client(shutdown["received"], private / "shutdown-1", "run-1")
    assert checked_clients == 47
    for name in (
        "tts_cancel_lifecycle.py",
        "cancel_recovery_server.py",
        "xvector_validation_server.py",
    ):
        assert ast.dump(ast.parse((private / "tools" / name).read_text())) == ast.dump(
            ast.parse((Path(__file__).parent / name).read_text())
        ), name
    start, end = [
        load(private / (name + "-environment.json")) for name in ("initial", "final")
    ]
    assert start["hashes"] == end["hashes"]
    assert [s.split(",", 1)[0] for s in start["pids"].splitlines()] == [
        s.split(",", 1)[0] for s in end["pids"].splitlines()
    ]
    assert end["owned_pids_absent"] and not end["ollama"]["models"]
    assert not start["ollama"]["models"]
    assert ":8766 " not in end["listeners"]
    assert all(end["health"][k] for k in ("stt", "tts", "turn"))
    before = load(private / "before-shutdown.json")
    after = load(private / "before-cleanup.json")
    assert all(
        s["pending"] == s["active"] == 0 and s["max_active"] == 1
        for s in (before, after)
    )
    for name, value in {
        "api": api,
        "browser": browser,
        "restart": restart,
        "server-main": streams,
        "server-restart": server_runs["run-2"],
        "shutdown": shutdown,
    }.items():
        save(data / (name + ".json"), scrub(value))
    save(
        data / "verification.json",
        {
            "status": "passed",
            "batch_date": "2026-10-21",
            "server_wav_files": checked,
            "client_wav_files": checked_clients,
            "api_cases": 36,
            "browser_bursts": 4,
            "browser_stops": 8,
            "max_active_model_calls": 1,
            "pending_after_tests": 0,
            "candidate_ast_matches_executed": True,
            "production_hashes_unchanged": True,
            "production_hashes": end["hashes"],
            "health": end["health"],
            "gpu_before": start["gpu"].strip(),
            "gpu_after": end["gpu"].strip(),
            "owned_pids_gone": True,
            "ollama_unchanged_empty": True,
        },
    )


def median(values):
    return statistics.median(values) if values else None


def summarize(data):
    api, browser, restart, servers = [
        load(data / (n + ".json")) for n in ("api", "browser", "restart", "server-main")
    ]
    result = {
        "counts": {
            "api_cases": len(api),
            "browser_bursts": len(browser),
            "intentional_faults": 8,
            "restart_controls": len(restart),
        },
        "arms": {},
        "forced_worker": [],
        "pcm_controls": [],
        "shutdown": load(data / "shutdown.json"),
    }
    for arm in ("v1", "fixed"):
        arm_rows = [r for r in api if r["arm"] == arm]
        group = {}
        for kind in sorted({r["kind"] for r in arm_rows}):
            rows = [r for r in arm_rows if r["kind"] == kind]
            metrics = {"n": len(rows)}
            if "followup" in rows[0]:
                assert all(r["followup"]["read_error"] is None for r in rows)
                metrics["followup_request_to_voiced_pcm_ms"] = median(
                    [r["followup"]["first_voiced_pcm_ms"] for r in rows]
                )
            if kind.startswith("disconnect-") or kind == "slow-cancel":
                targets = [
                    next(s for s in r["server"] if s["stream_id"] == r["target"])
                    for r in rows
                ]
                metrics["consumer_close_to_worker_exit_ms"] = median(
                    [
                        max(0, s["worker_done_ms"] - s["consumer_exit_ms"])
                        for s in targets
                    ]
                )
                metrics["model_starts"] = sum("producer_enter_ms" in s for s in targets)
                if kind == "disconnect-queued":
                    assert metrics["model_starts"] == 0
            if kind.startswith("fault-"):
                metrics["consumer_read_errors"] = sum(
                    r["target"]["read_error"] is not None for r in rows
                )
                assert metrics["consumer_read_errors"] == (
                    0 if arm == "v1" and kind == "fault-close" else 2
                )
            group[kind] = metrics
        controls = [r for r in arm_rows if r["kind"] == "control"]
        group["slow_controls"] = [
            {
                "case": r["case"],
                "send_delay_ms": r["send_delay_ms"],
                "queue_peak": r["server"][0]["queue_peak"],
                "lock_held_ms": r["server"][0]["lock_exit_ms"]
                - r["server"][0]["lock_enter_ms"],
                "request_to_voiced_pcm_ms": r["result"]["first_voiced_pcm_ms"],
            }
            for r in controls
        ]
        if arm == "fixed":
            assert all(
                s.get("queue_peak", 0) <= 8 for r in arm_rows for s in r["server"]
            )
        burst = [r for r in browser if r["policy"] == arm]
        assert len(burst) == 2 and all(
            r["late_sources"] == 0
            and r["followup_received"] == r["followup_samples"] == 43520
            for r in burst
        )
        for row in burst:
            assert row["max_active"] == 1 and len(row["stops"]) == 2
            for stop in row["stops"]:
                assert not any(
                    stop["at"] < s["called_at"] <= stop["gap_end"]
                    for s in row["sources"]
                )
            following = [s for s in row["sources"] if s["segment"] == 3]
            assert sum(s["frames"] for s in following) == 43520
            assert all(s["sample_rate"] == 16000 and "ended_at" in s for s in following)
            first_voice = min(
                s["first_voiced_at"]
                for s in following
                if s["first_voiced_at"] is not None
            )
            assert (
                abs(
                    first_voice
                    - row["followup_requested_at"]
                    - row["followup_to_voice_ms"]
                )
                < 0.001
            )
        group["browser"] = {
            "n": len(burst),
            "stops": 4,
            "late_sources": 0,
            "followup_request_to_scheduled_voice_ms": median(
                [r["followup_to_voice_ms"] for r in burst]
            ),
        }
        result["arms"][arm] = group
    for case in range(2):
        controls = [r for r in api if r["kind"] == "control" and r["case"] == case]
        digest = {r["result"]["pcm_sha256"] for r in controls}
        assert len(controls) == 4 and len(digest) == 1
        post = next(r for r in restart if r["case"] == case)
        assert (
            post["result"]["read_error"] is None
            and post["result"]["pcm_sha256"] in digest
        )
        result["pcm_controls"].append(
            {
                "case": case,
                "fast_slow_cross_arm_identical": True,
                "restart_identical": True,
                "pcm_sha256": digest.pop(),
            }
        )
    for row in api:
        for server in row["server"]:
            if "producer_exit_ms" in server:
                assert server["lock_exit_ms"] >= server["producer_exit_ms"]
        if row["kind"] == "force-worker":
            stopped = next(s for s in row["server"] if s["stream_id"] == row["target"])
            following = next(s for s in row["server"] if s["segment"] == "3")
            assert row["forced"]["phase"] == "running"
            assert (
                stopped["t0"] + stopped["lock_exit_ms"] / 1000
                <= following["t0"] + following["lock_enter_ms"] / 1000
            )
            result["forced_worker"].append(
                {
                    "cancel_to_producer_exit_ms": stopped["producer_exit_ms"]
                    - stopped["forced_cancel_ms"],
                    "lock_held_until_exit": True,
                }
            )
    for server in servers:
        if "producer_exit_ms" in server:
            assert server["lock_exit_ms"] >= server["producer_exit_ms"]
    stop = next(s for s in servers if s["trial"] == "graceful-shutdown")
    assert stop["producer_exit_ms"] <= stop["lock_exit_ms"] <= stop["worker_done_ms"]
    result["shutdown"]["consumer_close_to_producer_exit_ms"] = (
        stop["producer_exit_ms"] - stop["consumer_exit_ms"]
    )
    result["scope"] = (
        "HTTP PCM availability / WebAudio schedule; no new DSH/LLM or human mic/hardware measurement"
    )
    save(data / "summary.json", result)
    return result


def figure(folder, result):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(10, 4.3), layout="constrained")
    for j, (arm, color) in enumerate((("v1", "#59779c"), ("fixed", "#d37843"))):
        slow = [
            r for r in result["arms"][arm]["slow_controls"] if r["send_delay_ms"] == 40
        ]
        xs = [i + j * 0.3 for i in range(2)]
        axes[0].bar(xs, [r["queue_peak"] for r in slow], 0.28, color=color, label=arm)
        axes[1].bar(xs, [r["lock_held_ms"] / 1000 for r in slow], 0.28, color=color)
    axes[0].axhline(8, color="#333333", linestyle="--", lw=1)
    for ax in axes:
        ax.set_xticks([0.15, 1.15], ["Control A", "Control B"])
        ax.grid(axis="y", alpha=0.15)
    axes[0].set(
        title="Pending queue stays bounded",
        ylabel="Queue elements (includes end marker)",
    )
    axes[1].set(
        title="Tradeoff: slower sends hold the GPU lock longer",
        ylabel="Seconds holding inference lock",
    )
    axes[0].legend(frameon=False)
    fig.suptitle(
        "apex ICL recovery | 40 ms controlled send delay per PCM block | no crop",
        fontsize=12,
    )
    for ext in ("svg", "png"):
        fig.savefig(folder / ("recovery." + ext), dpi=160)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--private", type=Path)
    args = parser.parse_args()
    if args.private:
        import_evidence(args.private, args.folder)
    result = summarize(args.folder / "data")
    figure(args.folder, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
