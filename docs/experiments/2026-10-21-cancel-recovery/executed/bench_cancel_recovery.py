"""Real loopback HTTP probes. Never connects to a production mutation route."""

import argparse
import hashlib
import http.client
import json
import os
import signal
import socket
import time
import wave
from pathlib import Path

import numpy as np

SHORT = "好的，我们明天再聊吧。"
LONG = "我们先把今天的事情慢慢理清楚。第一件事是保存已经完成的记录，第二件事是检查明天需要准备的材料。如果你暂时还没有想好，也可以先休息一下，等精神好一点的时候再继续。最后记得把桌上的小盒子收好，我们明天再一起看看吧。"
NUMBER = "编号是一零零二零零三零零。"


class Probe:
    def __init__(self, folder):
        self.folder, self.rows = folder, []
        folder.mkdir(exist_ok=False)

    def api(self, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", 8766, timeout=10)
        try:
            conn.request(
                "GET" if body is None else "POST",
                path,
                body=None if body is None else json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            data = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"Private state/control {resp.status}: {data!r}")
            return json.loads(data)
        finally:
            conn.close()

    def open(self, key, arm, text=SHORT, seed=6501, delay=0, fault="none", segment=1):
        start = time.perf_counter()
        conn = http.client.HTTPConnection("127.0.0.1", 8766, timeout=15)
        conn.request(
            "POST",
            "/__voice_test__/tts/stream?policy=" + arm,
            json.dumps({"text": text}),
            headers={
                "Content-Type": "application/json",
                "X-Experiment-Trial": key,
                "X-Experiment-Seed": str(seed),
                "X-Experiment-Send-Delay-Ms": str(delay),
                "X-Experiment-Fault": fault,
                "X-Experiment-Segment": str(segment),
            },
        )
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"Private TTS {resp.status}: {resp.read()!r}")
        return {
            "key": key,
            "arm": arm,
            "id": resp.getheader("X-Test-Stream"),
            "conn": conn,
            "resp": resp,
            "started": start,
            "headers_ms": (time.perf_counter() - start) * 1000,
        }

    @staticmethod
    def disconnect(handle):
        handle["close_ms"] = (time.perf_counter() - handle["started"]) * 1000
        if handle["conn"].sock is not None:
            handle["conn"].sock.shutdown(socket.SHUT_RDWR)
        handle["resp"].close()
        handle["conn"].close()

    def read(self, handle, read_delay=0):
        audio, first, error = bytearray(), None, None
        try:
            while True:
                try:
                    part = handle["resp"].read(1024)
                except http.client.IncompleteRead as exc:
                    audio.extend(exc.partial)
                    raise
                if not part:
                    break
                audio.extend(part)
                if first is None:
                    values = np.frombuffer(
                        audio[: len(audio) // 2 * 2], dtype="<i2"
                    ).astype(np.int32)
                    if np.any(np.abs(values) >= 328):
                        first = (time.perf_counter() - handle["started"]) * 1000
                if read_delay:
                    time.sleep(read_delay)
        except (http.client.IncompleteRead, ConnectionError, OSError) as exc:
            error = type(exc).__name__
        finally:
            handle["resp"].close()
            handle["conn"].close()
        if len(audio) % 2:
            raise RuntimeError("Odd PCM byte count")
        with wave.open(str(self.folder / f"client-{handle['id']}.wav"), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(audio)
        return {
            "stream_id": handle["id"],
            "headers_ms": handle["headers_ms"],
            "first_voiced_pcm_ms": first,
            "total_ms": (time.perf_counter() - handle["started"]) * 1000,
            "received_samples": len(audio) // 2,
            "pcm_sha256": hashlib.sha256(audio).hexdigest(),
            "read_error": error,
        }

    def idle(self):
        deadline = time.perf_counter() + 15
        while True:
            state = self.api("/__voice_test__/state")
            if state["pending"] == 0 and state["active"] == 0:
                return state
            if time.perf_counter() > deadline:
                raise RuntimeError("Owned worker did not drain")
            time.sleep(0.03)

    def save(self, row):
        row["server"] = self.idle()["streams"]
        row["server"] = [s for s in row["server"] if s["trial"] == row["key"]]
        self.rows.append(row)
        (self.folder / "results.json").write_text(
            json.dumps(self.rows, ensure_ascii=False, indent=2)
        )
        print(row["kind"], row["arm"], row["key"], flush=True)

    def followup(self, key, arm):
        return self.read(self.open(key, arm, segment=3))

    def main_suite(self):
        for repeat in range(2):
            arms = ("v1", "fixed") if repeat == 0 else ("fixed", "v1")
            for kind in ("before", "after", "queued"):
                for arm in arms:
                    key = f"disconnect-{kind}-{repeat}-{arm}"
                    first = (
                        self.open(key, arm, LONG, 5501) if kind == "queued" else None
                    )
                    if first:
                        first["resp"].read(1024)
                    target = self.open(
                        key, arm, NUMBER, 4409, segment=2 if first else 1
                    )
                    if kind == "after":
                        target["resp"].read(1024)
                    self.disconnect(target)
                    if first:
                        time.sleep(0.08)
                        self.disconnect(first)
                    row = {
                        "kind": "disconnect-" + kind,
                        "key": key,
                        "arm": arm,
                        "target": target["id"],
                        "followup": self.followup(key, arm),
                    }
                    self.save(row)
            for arm in arms:
                key = f"slow-cancel-{repeat}-{arm}"
                target = self.open(key, arm, LONG, 5501, delay=40)
                target["resp"].read(1024)
                time.sleep(0.25)
                self.disconnect(target)
                self.save(
                    {
                        "kind": "slow-cancel",
                        "key": key,
                        "arm": arm,
                        "target": target["id"],
                        "followup": self.followup(key, arm),
                    }
                )
            for fault in ("generate", "close"):
                for arm in arms:
                    key = f"fault-{fault}-{repeat}-{arm}"
                    bad = self.read(self.open(key, arm, fault=fault))
                    self.save(
                        {
                            "kind": "fault-" + fault,
                            "key": key,
                            "arm": arm,
                            "target": bad,
                            "followup": self.followup(key, arm),
                        }
                    )
        for index, text in enumerate((SHORT, "我在这里陪着你呢。")):
            for arm in ("v1", "fixed"):
                for delay in (0, 40):
                    key = f"control-{index}-{arm}-{delay}"
                    control = self.read(
                        self.open(key, arm, text, 6501 + index, delay=delay)
                    )
                    self.save(
                        {
                            "kind": "control",
                            "key": key,
                            "arm": arm,
                            "case": index,
                            "send_delay_ms": delay,
                            "result": control,
                        }
                    )
        for arm in ("v1", "fixed"):
            key = f"wire-slow-{arm}"
            self.save(
                {
                    "kind": "wire-slow",
                    "key": key,
                    "arm": arm,
                    "result": self.read(self.open(key, arm), read_delay=0.025),
                }
            )
        for repeat in range(2):
            key = f"force-worker-{repeat}"
            target = self.open(key, "fixed", NUMBER, 4409)
            deadline = time.perf_counter() + 2
            while True:
                state = self.api("/__voice_test__/state")
                row = next(
                    s for s in state["streams"] if s["stream_id"] == target["id"]
                )
                if "producer_enter_ms" in row:
                    break
                if time.perf_counter() > deadline:
                    raise RuntimeError("Probe never entered generation")
                time.sleep(0.005)
            forced = self.api("/__voice_test__/worker-cancel/" + target["id"], {})
            self.disconnect(target)
            self.save(
                {
                    "kind": "force-worker",
                    "key": key,
                    "arm": "fixed",
                    "forced": forced,
                    "target": target["id"],
                    "followup": self.followup(key, "fixed"),
                }
            )

    def shutdown(self, manifest):
        pid = json.loads(manifest.read_text())["pid"]
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode()
        if "20261021-cancel-recovery/tools/cancel_recovery_server.py" not in cmd:
            raise RuntimeError("Refusing to signal an unowned process")
        target = self.open("graceful-shutdown", "fixed", NUMBER, 4409)
        started = time.perf_counter()
        os.kill(pid, signal.SIGTERM)
        received = self.read(target)
        deadline = time.perf_counter() + 15
        while Path(f"/proc/{pid}").exists():
            if time.perf_counter() > deadline:
                raise RuntimeError("Owned process did not exit")
            time.sleep(0.05)
        (self.folder / "shutdown.json").write_text(
            json.dumps(
                {
                    "pid": pid,
                    "signal_to_process_exit_ms": (time.perf_counter() - started) * 1000,
                    "received": received,
                    "process_absent": True,
                },
                indent=2,
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--phase", choices=("suite", "shutdown", "restart"), default="suite"
    )
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    probe = Probe(args.out)
    if args.phase == "suite":
        probe.main_suite()
    elif args.phase == "shutdown":
        if args.manifest is None:
            parser.error("Shutdown requires the owned manifest")
        probe.shutdown(args.manifest)
    else:
        for index, text in enumerate((SHORT, "我在这里陪着你呢。")):
            key = f"restart-{index}"
            probe.save(
                {
                    "kind": "restart",
                    "key": key,
                    "arm": "fixed",
                    "case": index,
                    "result": probe.read(probe.open(key, "fixed", text, 6501 + index)),
                }
            )


if __name__ == "__main__":
    main()
