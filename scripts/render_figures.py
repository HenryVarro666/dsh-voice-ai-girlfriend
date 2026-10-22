#!/usr/bin/env python3
"""Regenerate README figures from the archived experiment data; no GPU or network."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/assets"
OUT.mkdir(parents=True, exist_ok=True)
plt.rcParams.update(
    {"font.family": "DejaVu Sans", "font.size": 11, "svg.fonttype": "none"}
)
COLORS = ["#4378b9", "#a179c9", "#20a29a"]


def save(fig, name):
    fig.savefig(OUT / f"{name}.svg", bbox_inches="tight", facecolor=fig.get_facecolor())
    fig.savefig(
        OUT / f"{name}.png", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor()
    )
    plt.close(fig)


def latency():
    rows = json.loads(
        (ROOT / "docs/experiments/2026-10-20-e2e/results.json").read_text()
    )
    labels = [
        "New session / first reply",
        "Warm / complete question",
        "Warm / fallback timeout",
        "Warm / pause then continue",
    ]
    fig, ax = plt.subplots(figsize=(11.8, 4.8))
    fig.patch.set_facecolor("#f8fafc")
    ax.set_facecolor("#f8fafc")
    for i, row in enumerate(rows):
        events = [e for e in row["events"] if e["at"] >= 0]
        ready = [e["at"] + e["took"] for e in events if e["api"] in ("turn", "stt")][-1]
        tts = next(e["at"] for e in events if e["api"] == "tts/stream")
        play = next(e["at"] for e in events if e["api"] == "play")
        parts = [ready / 1000, (tts - ready) / 1000, (play - tts) / 1000]
        at = 0
        for j, value in enumerate(parts):
            ax.barh(
                i,
                value,
                left=at,
                color=COLORS[j],
                height=0.56,
                label=[
                    "Endpointing + ASR",
                    "DSH + LLM first clause",
                    "TTS + playback scheduling",
                ][j]
                if i == 0
                else None,
            )
            if value > 0.28:
                ax.text(
                    at + value / 2,
                    i,
                    f"{value:.2f}",
                    ha="center",
                    va="center",
                    color="white",
                    fontsize=10,
                )
            at += value
        ax.text(
            at + 0.04,
            i,
            f"{play / 1000:.2f} s",
            va="center",
            weight="bold",
            color="#142d49",
        )
    ax.set_yticks(range(len(rows)), labels)
    ax.invert_yaxis()
    ax.set_xlim(0, 3.25)
    ax.set_xlabel("Seconds after the last voiced input sample")
    ax.set_title(
        "Speech end to first voiced output",
        loc="left",
        fontsize=20,
        weight="bold",
        pad=36,
    )
    ax.text(
        0,
        1.04,
        "RTX 5080 16 GB  /  local 9B LLM  /  Qwen3 streaming TTS",
        transform=ax.transAxes,
        color="#506178",
    )
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", alpha=0.15)
    ax.set_axisbelow(True)
    ax.legend(
        loc="upper left", bbox_to_anchor=(0, -0.18), ncol=3, frameon=False, fontsize=9
    )
    fig.text(
        0.02,
        0.005,
        "4 synthetic-input trials, 2026-10-20. Browser scheduling measured; hardware output and real microphone untested. No p95 claim.",
        color="#506178",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    save(fig, "latency-breakdown")


def architecture():
    fig, ax = plt.subplots(figsize=(13, 6.5))
    fig.patch.set_facecolor("#f8fafc")
    ax.set(xlim=(0, 13), ylim=(0, 6.5))
    ax.axis("off")
    ax.text(
        0.3,
        6.1,
        "One GPU, a local conversational voice pipeline",
        size=21,
        weight="bold",
        color="#142d49",
    )
    ax.text(
        0.3,
        5.65,
        "Browser capture and playback; DSH owns the conversation and tools.",
        size=11,
        color="#506178",
    )

    def box(x, y, w, h, title, text, color):
        ax.add_patch(
            FancyBboxPatch(
                (x, y),
                w,
                h,
                boxstyle="round,pad=0.06,rounding_size=0.12",
                facecolor=color,
                edgecolor="none",
            )
        )
        ax.text(x + 0.18, y + h - 0.35, title, size=13, weight="bold", color="white")
        ax.text(
            x + 0.18,
            y + h - 0.7,
            text,
            va="top",
            size=10,
            color="white",
            linespacing=1.6,
        )

    def arrow(x1, y1, x2, y2, label=""):
        ax.annotate(
            "",
            (x2, y2),
            (x1, y1),
            arrowprops={"arrowstyle": "->", "lw": 1.8, "color": "#506178"},
        )
        if label:
            ax.text(
                (x1 + x2) / 2,
                (y1 + y2) / 2 + 0.13,
                label,
                ha="center",
                size=9,
                color="#506178",
            )

    box(
        0.35,
        2.75,
        3.3,
        2.3,
        "Browser plugin",
        "Mic + AudioWorklet\n500 ms pause check\nSentence splitter + PCM player\nBounded pre-roll + barge-in",
        "#284b70",
    )
    box(
        4.65,
        3.1,
        3.4,
        1.95,
        "Voice bridge  :8765",
        "FunASR speech recognition\nQwen3-0.6B turn scorer (CPU)\nQwen3-TTS streaming (GPU)",
        "#237e7b",
    )
    box(
        9.05,
        3.1,
        3.55,
        1.95,
        "DSH  :3080",
        "Session and tool orchestration\nOllama: Qwen3.5 9B (GPU)\nReasoning disabled in voice mode",
        "#735398",
    )
    arrow(3.7, 4.55, 4.58, 4.55, "PCM")
    arrow(4.58, 3.1, 3.7, 3.1, "PCM stream")
    # Session traffic goes through the browser, not directly bridge -> DSH.
    ax.plot([2, 2, 10.8, 10.8], [5.1, 5.32, 5.32, 5.1], color="#506178", lw=1.6)
    arrow(10.8, 5.32, 10.8, 5.1)
    ax.text(
        6.4,
        5.4,
        "Recognized text -> DSH session prompt",
        ha="center",
        size=9,
        color="#506178",
    )
    ax.plot([10.8, 10.8, 2], [3.02, 2.5, 2.5], color="#506178", lw=1.6)
    arrow(2, 2.5, 2, 2.7)
    ax.text(
        6.4,
        2.32,
        "DSH reply text -> browser sentence splitter",
        ha="center",
        size=9,
        color="#506178",
    )
    arrow(3.7, 3.85, 4.58, 3.85, "sentence")
    ax.add_patch(
        FancyBboxPatch(
            (0.35, 0.52),
            12.25,
            1.08,
            boxstyle="round,pad=0.06",
            facecolor="#e5edf4",
            edgecolor="none",
        )
    )
    ax.text(0.58, 1.22, "Interrupt path", weight="bold", color="#284b70", size=12)
    ax.text(
        0.58,
        0.84,
        "Mic -> WebSocket Silero VAD -> stop playback + abort current TTS; keep input pre-roll.",
        color="#284b70",
        size=10,
    )
    ax.text(
        0.4,
        0.12,
        "DUIX avatar and QQ integrations are optional. Full-duplex speech models are not part of this verified pipeline.",
        size=9,
        color="#506178",
    )
    save(fig, "architecture")


if __name__ == "__main__":
    latency()
    architecture()
    print(f"Figures written to {OUT}")
