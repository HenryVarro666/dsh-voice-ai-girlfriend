"""Summarize repeated checks per immutable audio, retaining every missing call."""

from collections import Counter, defaultdict


def repeated_summary(suite, rows):
    expected = {item["id"]: item for item in suite}
    if len(expected) != len(suite):
        raise ValueError("Duplicate suite IDs")
    grouped, seen = defaultdict(list), set()
    for row in rows:
        key = (row["id"], row["repeat"])
        if row["id"] not in expected or key in seen:
            raise ValueError("Unknown or duplicate ASR repeat")
        item = expected[row["id"]]
        if not 0 <= row["repeat"] < item["repeats"]:
            raise ValueError("ASR repeat index is outside the frozen protocol")
        if item.get("sha256", row["wav_sha256"]) != row["wav_sha256"]:
            raise ValueError("Repeated ASR is not bound to the expected WAV")
        seen.add(key)
        grouped[row["id"]].append(row)
    result = {}
    for key, item in expected.items():
        samples = grouped[key]
        complete = len(samples) == item["repeats"] and all(
            "error" not in r for r in samples
        )
        result[key] = {
            "planned_calls": item["repeats"],
            "calls": len(samples),
            "errors": sum("error" in r for r in samples),
            "missing": item["repeats"] - len(samples),
            "variants": len(
                {r["normalized_asr_sha256"] for r in samples if "error" not in r}
            ),
            "stable_exact": complete and all(r["exact"] for r in samples),
            "exact_calls": sum(r.get("exact", False) for r in samples),
        }
        if "case" in item:
            statuses = Counter(r.get("field_status", "request_error") for r in samples)
            result[key].update(
                field_status_counts=dict(statuses),
                observed_code_counts=dict(
                    Counter(str(r.get("observed_code")) for r in samples)
                ),
                stable_fields=complete
                and all(r.get("field_status") == "match" for r in samples),
                any_fields_match=any(r.get("field_status") == "match" for r in samples),
                field_result_changes=len(
                    {r.get("field_status") for r in samples if "error" not in r}
                )
                > 1,
            )
    return result
