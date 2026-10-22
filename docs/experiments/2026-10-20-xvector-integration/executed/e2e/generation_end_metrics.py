"""Pure CPU summaries for the isolated generation-end experiment."""

import numpy as np


def classify_stop(sampled_ids, eos_id, frames, token_cap):
    if frames >= token_cap:
        return "max_tokens"
    if sampled_ids and sampled_ids[-1] == eos_id:
        return "eos"
    return "other"


def audio_stats(audio, sr, threshold):
    values = np.asarray(audio).reshape(-1)
    active = np.flatnonzero(np.abs(values) >= threshold)
    return {
        "duration_ms": len(values) / sr * 1000,
        "leading_ms": int(active[0]) / sr * 1000 if len(active) else None,
        "tail_ms": (len(values) - int(active[-1]) - 1) / sr * 1000
        if len(active)
        else None,
        "all_quiet": not bool(len(active)),
    }


def choose_candidate(baseline, candidates):
    priority = {"temperature_05": 0, "repetition_115": 1, "eos_bias_1": 2}
    eligible = [
        (row["long_tails"], row["tail_ms"], priority[name], name)
        for name, row in candidates.items()
        if row["eos"]
        and row["edits"] <= baseline["edits"]
        and (row["long_tails"], row["tail_ms"])
        < (baseline["long_tails"], baseline["tail_ms"])
    ]
    return min(eligible)[-1] if eligible else None
