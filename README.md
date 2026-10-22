# DSH Voice AI

**English** · [简体中文](README.zh-CN.md)

**Interruptible Chinese voice access to a tool-capable assistant on one consumer GPU.**

## Problem and intended product

Typing breaks hands-free interaction with a desktop assistant. This project adds continuous speech capture, spoken replies and interruption to DeepSeek Harness (DSH), while DSH retains conversation, tools and session state.

The target experience is to ask naturally, hear a reply promptly, and interrupt without losing the beginning of the correction or replaying obsolete speech. The current work measures where that experience fails before changing defaults.

## Approach

```mermaid
flowchart LR
  Mic[Browser capture] --> ASR[FunASR]
  ASR --> Turn[Text endpointing]
  Turn --> DSH[DSH and local 9B]
  DSH --> TTS[Qwen3 streaming TTS]
  TTS --> Play[Browser PCM scheduling]
  Mic -. Silero VAD interruption .-> Play
```

The Python bridge owns speech models and protocols; the [standalone plugin](https://github.com/HenryVarro666/dsh-voice-ai-girlfriend-plugin) owns capture, submission and playback. The LLM runs separately and may compete with TTS for GPU resources. An avatar adapter exists, but real DUIX rendering has not been accepted here.

## Current state

- A Chinese ASR → DSH/local Qwen3.5 9B → Qwen3-TTS 0.6B chain has run on an RTX 5080 16 GB.
- Playback/capture repairs cover first replies, newline-preserving speech, bounded idle pre-roll, stale results and unavailable-avatar fallback.
- Local source includes refactoring and isolated cancellation candidates that differ from the last measured deployed build. Publishing this repository does not deploy them.
- Real microphones, loudspeaker echo, long-session stability and clean-machine installation still need acceptance. This is a cascaded speech pipeline with interruption, not a native full-duplex model.

## Results and decisions

| Investigation | Supported observation | Decision |
|---|---|---|
| [17-turn complete chain](docs/experiments/2026-10-20-player-validation/RESULTS.md) | New-session first request 3.75 s; 16 warm turns 1.04–2.01 s | Keep baseline; no stable p95 claim |
| [ICL cancellation lifecycle](docs/experiments/2026-10-20-cancel-lifecycle/RESULTS.md) | Before-first-PCM consumer-close → generation-exit median 1948.4 → 63.3 ms; cancelled queued jobs invoked model 0/2 | Useful isolated fix, not normal-turn speedup; not deployed |
| [Cancellation/recovery stress](docs/experiments/2026-10-21-cancel-recovery/RESULTS.md) | 36 HTTP cases and eight programmed player stops; queue peaks 70/38 → 8/8 | Bounded backlog helps, but stalled senders can still hold the lock indefinitely |
| [Model size and tail guard](docs/experiments/2026-10-20-stream-guard/RESULTS.md) | 1.7B had no general first-voice benefit and used about 2 GiB more; amplitude rules cut quiet speech | Retain 0.6B and disabled cropping |
| [Generation conditioning](docs/experiments/2026-10-20-generation-layout/RESULTS.md) | Speaker-vector-only mode improves isolated handler timing, but changes voice conditioning | Keep experimental pending voice/prosody/content review |
| [Chinese endpointing](docs/experiments/2026-10-19-turn/RESULTS.md) | Synthetic and read-speech results disagree; semantic completion can cut continued speech | Do not assume model endpointing beats silence timing in live conversation |

Complete-chain timing starts at the last voiced synthetic input sample and ends at scheduled first voiced output in AudioContext. It excludes hardware output and real-microphone/AEC validation. First session is not controlled cold start. Cancellation and isolated handler timings have different endpoints.

## Experiment record

[Decision index](docs/EXPERIMENT_INDEX.md) is the short reading path. [Detailed experiments](docs/EXPERIMENTS.md) retain methods and prior findings; individual experiment folders contain plans, numeric summaries, scripts and limitations. Negative results and rejected candidates remain visible.

## Build and use

[Setup](docs/SETUP.md) documents model acquisition and local configuration. Source-only CPU checks do not require weights:

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s bridge/tests -v
python scripts/check_docs.py
```

The browser bundle is built in the companion plugin repository. For optional monorepo integration, use `dsh-plugin/`; its `src` mirrors the standalone plugin but its package/build layout differs. [Architecture](docs/ARCHITECTURE.md) · [Contributing](CONTRIBUTING.md).

## Next work

1. Define sender-stall and whole-request lock budgets without releasing inference ownership before the worker exits.
2. Complete shared-lock compatibility and actual candidate replacement/recovery checks.
3. Run natural microphone/headphone and loudspeaker/AEC cases, then enlarge latency samples.
4. Revisit endpointing and TTS conditioning only with frozen quality and failure controls. [Design and acceptance roadmap](docs/DESIGN_ROADMAP.md).

## Origin and publication

Derived from [beiyege-01/dsh-voice-ai-girlfriend](https://github.com/beiyege-01/dsh-voice-ai-girlfriend). This is a source-only publication with upstream attribution and license preserved; upstream history containing raw video is not republished. [Provenance and exclusions](PUBLICATION.md).

No weights, credentials or raw/generated audio-video are included. Model and media rights remain separate. System guides are mainly Chinese; both overview languages share the same evidence. [Workspace map](https://github.com/HenryVarro666/voice-ai-workspace).
