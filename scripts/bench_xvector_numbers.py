#!/usr/bin/env python3
"""Run the frozen numeric cases with only ICL vs speaker-vector conditioning."""

import argparse
import json
import sys
from pathlib import Path

import bench_generation_layout as layout
from number_reading import render


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--cases", required=True, type=Path)
    args, remaining = parser.parse_known_args()
    cases = json.loads(args.cases.read_text())
    layout.TEXTS = [render(c, True) for c in cases["diagnostic"]]
    layout.HOLDOUT = [render(c, True) for c in cases["holdout"]]
    layout.PROFILES = {"baseline": {}, "xvector": {"xvec_only": True}}
    sys.argv = [sys.argv[0], *remaining]
    layout.main(
        diagnostic_seeds=cases["diagnostic_seeds"],
        holdout_seeds=cases["holdout_seeds"],
    )


if __name__ == "__main__":
    main()
