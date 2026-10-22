"""Verify original PCM, browser counts, skipped jobs and production restoration."""

import argparse
import ast
import hashlib
import json
import wave
from pathlib import Path

from integration_metrics import e2e_metrics
from report_xvector_integration import cancel_metrics, queue_metrics


def load(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private", type=Path, required=True)
    parser.add_argument("--public", type=Path, required=True)
    args = parser.parse_args()
    raw, public = args.private / "run-3", args.public
    cancel = load(raw / "browser-interruption.json")
    queue = load(raw / "browser-player.json")
    e2e = load(raw / "browser-e2e-combined.json")
    assert (len(cancel), len(queue), len(e2e)) == (12, 8, 18)
    skipped = []
    for row in cancel:
        metric = cancel_metrics(row)
        assert "error" not in metric and metric["late_sources"] == 0
        server = next(
            s for s in row["server"] if s["segment"] == str(row["stopped_segment"])
        )
        if row["policy"] == "fixed" and row["stopped_segment"] == 2:
            assert metric["queue_skipped"] and not metric["model_started"]
            assert not any(
                k in server
                for k in ("lock_enter_ms", "producer_enter_ms", "first_raw_ms")
            )
            assert server.get("sent_samples", 0) == 0
            skipped.append(server["stream_id"])
        else:
            assert server["lock_exit_ms"] >= server["producer_exit_ms"]
            assert server["cancel_set_at_exit"] is True
    assert len(skipped) == 2
    bounds = load(public / "data/queue-audio-bounds.json")
    pairs = {}
    for row in queue:
        metric = queue_metrics(row, bounds)
        assert metric["received_samples"] == metric["scheduled_samples"]
        for server in row["server"]:
            path = raw / "streams" / f"{server['stream_id']}.wav"
            pairs.setdefault((row["key"], server["segment"]), []).append(
                hashlib.sha256(path.read_bytes()).hexdigest()
            )
    assert len(pairs) == 8 and all(len(v) == 2 and v[0] == v[1] for v in pairs.values())
    for row in e2e:
        metric = e2e_metrics(row)
        assert "error" not in metric and metric["tts_errors"] == 0
        assert metric["received_samples"] == metric["scheduled_samples"] > 0
    servers = load(raw / "server-streams.json")
    waves = list((raw / "streams").glob("*.wav"))
    assert len(waves) == len(servers)
    for server in servers:
        with wave.open(str(raw / "streams" / f"{server['stream_id']}.wav")) as wav:
            assert wav.getparams()[:3] == (1, 2, 16000)
            assert wav.getnframes() == server.get("sent_samples", 0)
            assert len(wav.readframes(wav.getnframes())) == wav.getnframes() * 2
    controls = load(args.private / "controls/controls.json")
    assert len(controls) == 8
    for case in range(4):
        pair = [r for r in controls if r["case"] == case]
        assert len(pair) == 2 and pair[0]["pcm_sha256"] == pair[1]["pcm_sha256"]
        for row in pair:
            with wave.open(str(raw / "streams" / f"{row['stream_id']}.wav")) as wav:
                assert (
                    hashlib.sha256(wav.readframes(wav.getnframes())).hexdigest()
                    == row["pcm_sha256"]
                )
    for name in (
        "tts_cancel_lifecycle.py",
        "cancel_validation_server.py",
        "xvector_validation_server.py",
    ):
        # Historical evidence binds to its frozen source, not today's candidate.
        actual, local = args.private / "tools" / name, public / "executed" / name
        assert ast.dump(ast.parse(actual.read_text())) == ast.dump(
            ast.parse(local.read_text())
        ), name
    env = load(args.private / "final-environment.json")
    assert env["owned_pid_absent"] and not env["ollama"]["models"]
    assert all(env["health"][k] is True for k in ("stt", "tts", "turn"))
    assert env["hashes"] == {
        "bridge": "d8b527cf94e762c5d2bf7ee5ba762ae3861cea72c04f6bc7b841229f69649576",
        "config": "68dcd0a74297ad2179e42494a80ad125496db3b0aad15a2c3458f46bd4835f70",
        "plugin": "4519465705834132671dced9ddd784d9ad519355754ac02e985cb5dbc2e28f74",
    }
    assert load(args.private / "before-cleanup.json")["pending"] == 0
    result = {
        "status": "passed",
        "verified_date": "2026-10-20",
        "cancel_cases": 12,
        "cancel_late_sources": 0,
        "skipped_model_calls": 2,
        "queued_fixed_model_starts": 0,
        "queue_pcm_identical_pairs": 8,
        "frozen_clause_pcm_identical_pairs": 4,
        "e2e_samples_equal": 18,
        "checked_wav_files": len(waves),
        "candidate_ast_matches_executed": True,
        "production_hashes": env["hashes"],
        "health": env["health"],
        "gpu_after": env["gpu"].strip(),
        "owned_process_gone": True,
        "pending_before_cleanup": 0,
        "ollama_empty": True,
    }
    (public / "data/verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
