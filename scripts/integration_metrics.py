"""Clock-safe metrics from recorded browser events and immutable PCM bounds."""

import hashlib


def e2e_metrics(row):
    if "error" in row:
        return {"error": row["error"]}
    tts = [e for e in row["events"] if e["api"] == "tts/stream"]
    if not tts or not row.get("first_voice_at"):
        return {"error": "No answer speech"}
    first = tts[0]
    return {
        "path": "fast"
        if any(
            e.get("result", {}).get("complete") is True
            for e in row["events"]
            if e["api"] == "turn"
        )
        else "fallback",
        "voice_to_voice_ms": row["first_voice_at"] - row["input_end"],
        "before_tts_ms": first["at"] - row["input_end"],
        "tts_to_voice_ms": row["first_voice_at"] - first["at"],
        "first_clause_sha256": first.get("text_sha256")
        or hashlib.sha256(first["text"].encode()).hexdigest(),
        "first_clause_chars": first.get("text_chars", len(first.get("text", ""))),
        "received_samples": sum(s.get("sent_samples", 0) for s in row["server"]),
        "scheduled_samples": sum(s["frames"] for s in row["sources"]),
        "tts_errors": sum(e.get("status", 0) >= 400 for e in tts),
    }


def cancel_metrics(row):
    servers = [s for s in row["server"] if s["segment"] == str(row["stopped_segment"])]
    if len(servers) != 1 or not (
        "producer_exit_ms" in servers[0] or "worker_exit_ms" in servers[0]
    ):
        return {"error": "Missing stopped producer trace"}
    server = servers[0]
    queued = row["stopped_segment"] == 2
    sources = [
        s
        for s in row["sources"]
        if not queued
        or (
            s["context_id"] != row["first_context_id"]
            and s["called_at"] < row["followup_requested_at"]
        )
    ]
    ended = [
        s["ended_at"] for s in sources if s.get("stopped_at") and s.get("ended_at")
    ]
    result = {
        "late_sources": row["late_sources_in_stopped_segment"],
        "sources_before_stop": row.get("sources_before_stop"),
        "playback_stop_ms": max(0, max(ended) - row["stop_requested_at"])
        if ended
        else None,
        "consumer_close_to_producer_exit_ms": max(
            0,
            server.get("producer_exit_ms", server.get("worker_exit_ms"))
            - server["consumer_exit_ms"],
        ),
        "cancelled_request_computed_after_lock": (
            server["producer_exit_ms"] - server["producer_enter_ms"]
            if "producer_enter_ms" in server
            else 0
        ),
        "model_started": "producer_enter_ms" in server,
        "queue_skipped": "queue_skipped_ms" in server,
        "lock_exit_recorded": "lock_exit_ms" in server,
    }
    if queued:
        followup = [
            s["first_voiced_at"]
            for s in row["sources"]
            if s["context_id"] != row["first_context_id"]
            and s["called_at"] >= row["followup_requested_at"]
            and s["first_voiced_at"] is not None
        ]
        result["followup_request_to_voice_ms"] = (
            min(followup) - row["followup_requested_at"] if followup else None
        )
    return result


def queue_metrics(row, bounds):
    """Restore clip boundaries by exact PCM counts, not mutable UI labels."""
    sources = sorted(row["sources"], key=lambda x: x["called_at"])
    index, clips = 0, []
    for server in row["server"]:
        audio = bounds[server["stream_id"]]
        remaining, offset, mapped, clip = audio["samples"], 0, {}, []
        if audio["samples"] != server["sent_samples"]:
            raise ValueError("Saved PCM and HTTP counts differ")
        while remaining > 0:
            if index == len(sources):
                raise ValueError("Player scheduled fewer samples than received")
            source = sources[index]
            index += 1
            remaining -= source["frames"]
            if remaining < 0:
                raise ValueError("A player buffer crosses a clip boundary")
            clip.append(source)
            for name in ("first", "last"):
                sample = audio[f"{name}_voiced_sample"]
                if offset <= sample < offset + source["frames"]:
                    mapped[name] = (
                        source["scheduled_start"]
                        + (sample - offset) * 1000 / source["sample_rate"]
                    )
            offset += source["frames"]
        if not clip or set(mapped) != {"first", "last"}:
            raise ValueError("Missing clip or voiced boundary")
        clips.append(
            mapped
            | {"start": clip[0]["scheduled_start"], "end": clip[-1]["scheduled_end"]}
        )
    if len(clips) != 2 or index != len(sources):
        raise ValueError("Queue must contain exactly two complete clips")
    return {
        "playback_total_ms": row["last_ended_at"] - row["started"],
        "interclip_schedule_gap_ms": clips[1]["start"] - clips[0]["end"],
        "interclip_voiced_gap_ms": clips[1]["first"] - clips[0]["last"],
        "received_samples": sum(s["sent_samples"] for s in row["server"]),
        "scheduled_samples": sum(s["frames"] for s in sources),
    }
