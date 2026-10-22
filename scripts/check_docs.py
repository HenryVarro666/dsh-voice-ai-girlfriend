#!/usr/bin/env python3
"""Check repository-relative links in the maintained public documentation."""

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "docs/experiments/2026-10-21-cancel-recovery/PLAN.md",
    "docs/experiments/2026-10-21-cancel-recovery/RESULTS.md",
    "docs/experiments/2026-10-20-cancel-lifecycle/PLAN.md",
    "docs/experiments/2026-10-20-cancel-lifecycle/RESULTS.md",
    "docs/experiments/2026-10-20-xvector-integration/PLAN.md",
    "docs/experiments/2026-10-20-xvector-integration/RESULTS.md",
    "docs/experiments/2026-10-20-numeric-recheck/PLAN.md",
    "docs/experiments/2026-10-20-numeric-recheck/RESULTS.md",
    "docs/experiments/2026-10-20-numeric-recheck/METRICS.md",
    "docs/experiments/2026-10-20-xvector-listening/RATING_IMPORT.md",
    "docs/experiments/2026-10-20-xvector-listening/PLAN.md",
    "docs/experiments/2026-10-20-xvector-listening/RESULTS.md",
    "docs/experiments/2026-10-20-generation-layout/PLAN.md",
    "docs/experiments/2026-10-20-generation-layout/RESULTS.md",
    "docs/experiments/2026-10-20-generation-layout/METRICS.md",
    "docs/experiments/2026-10-20-number-reading/PLAN.md",
    "docs/experiments/2026-10-20-number-reading/RESULTS.md",
    "docs/experiments/2026-10-20-repetition-validation/PLAN.md",
    "docs/experiments/2026-10-20-repetition-validation/RESULTS.md",
    "docs/experiments/2026-10-20-generation-end/PLAN.md",
    "docs/experiments/2026-10-20-generation-end/RESULTS.md",
    "README.md",
    "README.zh-CN.md",
    "CONTRIBUTING.md",
    "dsh-plugin/README.md",
    "dsh-plugin/README.zh-CN.md",
    "docs/SETUP.md",
    "docs/ARCHITECTURE.md",
    "docs/EXPERIMENTS.md",
    "docs/DESIGN_ROADMAP.md",
    "docs/TTS_MODELS.md",
    "docs/BUG_FIX_LOG.md",
    "docs/experiments/2026-10-19-turn/README.md",
    "docs/experiments/2026-10-19-turn/RESULTS.md",
    "docs/experiments/2026-10-20-tts-contention/PLAN.md",
    "docs/experiments/2026-10-20-tts-contention/RESULTS.md",
    "docs/experiments/2026-10-20-tts-contention/METRICS.md",
    "docs/experiments/2026-10-20-first-clause/PLAN.md",
    "docs/experiments/2026-10-20-first-clause/RESULTS.md",
    "docs/experiments/2026-10-20-first-clause/METRICS.md",
    "docs/experiments/2026-10-20-tts-tail/PLAN.md",
    "docs/experiments/2026-10-20-tts-tail/RESULTS.md",
    "docs/experiments/2026-10-20-tts-tail/METRICS.md",
    "docs/experiments/2026-10-20-stream-guard/PLAN.md",
    "docs/experiments/2026-10-20-stream-guard/RESULTS.md",
    "docs/experiments/2026-10-20-stream-guard/METRICS.md",
    "docs/experiments/2026-10-20-player-validation/PLAN.md",
    "docs/experiments/2026-10-20-player-validation/RESULTS.md",
    "docs/experiments/2026-10-20-player-validation/METRICS.md",
    "docs/experiments/2026-10-20-tail-safety/PLAN.md",
    "docs/experiments/2026-10-20-tail-safety/RESULTS.md",
]


def main() -> int:
    errors = []
    for name in FILES:
        path = ROOT / name
        for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", path.read_text()):
            if "://" in target or target.startswith("#"):
                continue
            target = unquote(target.split("#", 1)[0])
            if not (path.parent / target).exists():
                errors.append(f"{name}: {target}")
    for error in errors:
        print(f"Broken link: {error}")
    print(
        f"Checked {len(FILES)} maintained documents; {len(errors)} broken local links."
    )
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
