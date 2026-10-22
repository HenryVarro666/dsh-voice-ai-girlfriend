#!/usr/bin/env python3
"""Decode a verified listening export without treating missing votes as passes."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

CHOICES = {
    "timbre": {"pending", "A", "B", "same", "both_bad", "unsure"},
    "prosody": {"pending", "A", "B", "same", "both_bad", "unsure"},
    "ending": {"pending", "both_ok", "A_bad", "B_bad", "A", "B", "both_bad", "unsure"},
}


def analyze(export, mapping):
    pairs = mapping["pairs"]
    actual_hash = hashlib.sha256(
        json.dumps(pairs, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    if mapping["mapping_sha256"] != actual_hash:
        raise ValueError("Local A/B mapping hash does not match its contents")
    if export.get("version") != 1 or export.get("pack_id") != mapping["pack_id"]:
        raise ValueError("Wrong export version or listening pack")
    if export.get("mapping_sha256") != actual_hash:
        raise ValueError("Export A/B mapping differs from this listening pack")
    if not isinstance(export.get("revealed"), bool):
        raise TypeError("Export must record whether the page had been revealed")
    indexed = {p["id"]: p for p in pairs}
    if len(indexed) != len(pairs):
        raise ValueError("Duplicate IDs in the A/B mapping")
    ratings = {}
    for item in export["ratings"]:
        pair_id = item["id"]
        if pair_id not in indexed or pair_id in ratings:
            raise ValueError(f"Unknown or duplicated rating ID: {pair_id}")
        row = {field: item.get(field, "pending") for field in CHOICES}
        if any(row[field] not in choices for field, choices in CHOICES.items()):
            raise ValueError(f"Unknown rating choice: {pair_id}")
        notes = item.get("notes", "")
        if not isinstance(notes, str):
            raise TypeError(f"Notes must be text: {pair_id}")
        if "QA技术验证，非人工评分" in notes:
            raise ValueError("Export contains technical QA values, not human ratings")
        ratings[pair_id] = row | {"notes": notes}
    decoded = []
    for pair in pairs:
        row = ratings.get(pair["id"], dict.fromkeys(CHOICES, "pending") | {"notes": ""})
        outcomes = {}
        for field in CHOICES:
            value = row[field]
            if value in ("A", "B"):
                mode = pair["mapping"][value]["mode"]
                outcomes[field] = mode + "_preferred"
            elif value in ("A_bad", "B_bad"):
                mode = pair["mapping"][value[0]]["mode"]
                outcomes[field] = mode + "_ending_problem"
            else:
                outcomes[field] = value
        decoded.append(
            {
                "id": pair["id"],
                "source": pair["source"],
                "priority": pair["priority"],
                "text": pair["text"],
                "seed": pair["seed"],
                "absent_from_export": pair["id"] not in ratings,
                "ratings": row,
                "outcomes": outcomes,
            }
        )
    groups = {}
    for name, selected in (
        ("all", decoded),
        ("priority", [r for r in decoded if r["priority"]]),
        ("new_endings", [r for r in decoded if r["source"] == "endings"]),
    ):
        groups[name] = {
            "pairs": len(selected),
            "completed_pairs": sum(
                all(r["ratings"][f] != "pending" for f in CHOICES) for r in selected
            ),
            "pairs_with_any_choice": sum(
                any(r["ratings"][f] != "pending" for f in CHOICES) for r in selected
            ),
            "notes_only_pairs": sum(
                bool(r["ratings"]["notes"])
                and all(r["ratings"][f] == "pending" for f in CHOICES)
                for r in selected
            ),
            "dimensions": {
                field: dict(Counter(r["outcomes"][field] for r in selected))
                for field in CHOICES
            },
        }
    has_choices = bool(groups["all"]["pairs_with_any_choice"])
    return {
        "pack_id": mapping["pack_id"],
        "mapping_sha256": actual_hash,
        "exported_at": export.get("exported_at"),
        "revealed_at_export": export["revealed"],
        "export_rows": len(ratings),
        "absent_rows": [r["id"] for r in decoded if r["absent_from_export"]],
        "evidence_status": "human_choices_present"
        if has_choices
        else "no_human_choices",
        "groups": groups,
        "pairs": decoded,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ratings", required=True, type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = analyze(
        json.loads(args.ratings.read_text()), json.loads(args.mapping.read_text())
    )
    result["ratings_sha256"] = hashlib.sha256(args.ratings.read_bytes()).hexdigest()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "pairs"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
