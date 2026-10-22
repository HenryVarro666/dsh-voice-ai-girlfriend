#!/usr/bin/env python3
"""Check/copy standalone plugin src into this repo's monorepo source mirror."""

import argparse
import filecmp
import shutil
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=root.parent / "dsh-voice-ai-girlfriend-plugin/src",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    destination = root / "dsh-plugin/src"
    if not args.source.is_dir():
        parser.error(f"Plugin source directory not found: {args.source}")
    source_files = {
        p.relative_to(args.source) for p in args.source.rglob("*") if p.is_file()
    }
    mirror_files = {
        p.relative_to(destination) for p in destination.rglob("*") if p.is_file()
    }
    if extra := sorted(mirror_files - source_files):
        parser.error(f"Mirror has extra files; review them before deleting: {extra}")
    changed = [
        p
        for p in sorted(source_files)
        if not (destination / p).exists()
        or not filecmp.cmp(args.source / p, destination / p, shallow=False)
    ]
    for relative in changed:
        print(f"{'copy' if args.apply else 'diff'}: {relative}")
        if args.apply:
            (destination / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(args.source / relative, destination / relative)
    if not changed:
        print("Plugin source mirror is in sync.")
    return int(bool(changed) and not args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
