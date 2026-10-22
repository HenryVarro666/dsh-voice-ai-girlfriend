# Experiment decisions / 实验决策索引

Read this index before the chronological detail. Each linked directory preserves its plan, result, source/config hashes, failed attempts and limitations. Detailed reports are primarily Chinese.

**Current defaults:** last measured deployment remains default ICL, full-text conditioning, chunk4, repetition penalty1.05 and no cropping. Local refactoring/cancellation candidates are not automatically deployed by publication.

| Record | Question | Evidence | Decision / next |
|---|---|---|---|
| [2026-10-19-turn](experiments/2026-10-19-turn/RESULTS.md) | Chinese end-of-turn | Synthetic/read-speech evaluation does not establish live superiority | Keep real-conversation acceptance open |
| [2026-10-20-cancel-lifecycle](experiments/2026-10-20-cancel-lifecycle/RESULTS.md) | Cancellation ownership | 1948.4→63.3ms before-first-PCM exit; queued cancel0/2 model starts | Isolated repair; default ICL; not deployed |
| [2026-10-20-first-clause](experiments/2026-10-20-first-clause/RESULTS.md) | First-clause splitting | 36 trials/96 segment requests; no stable replacement winner | Keep current splitting |
| [2026-10-20-generation-end](experiments/2026-10-20-generation-end/RESULTS.md) | EOS/sampling | Known tail improves at repetition penalty1.15; held-out general speed gain absent | Keep experimental; listening pending |
| [2026-10-20-generation-layout](experiments/2026-10-20-generation-layout/RESULTS.md) | Conditioning/chunk layout | 80 outputs; vector-only faster in handler; chunk2 not beneficial | Voice/prosody/content acceptance before adoption |
| [2026-10-20-number-reading](experiments/2026-10-20-number-reading/RESULTS.md) | Number normalization | Independent text/audio checks retain ambiguous numeric cases | Do not relax exact numeric facts |
| [2026-10-20-numeric-recheck](experiments/2026-10-20-numeric-recheck/RESULTS.md) | Repeated-digit ambiguity | Frozen original waveforms and repeated ASR disagree | Retain unresolved TTS/ASR attribution |
| [2026-10-20-player-validation](experiments/2026-10-20-player-validation/RESULTS.md) | Real browser/player chain | 31 player cases +17 DSH turns with synthetic capture | Production remains uncropped; microphone/hardware pending |
| [2026-10-20-repetition-validation](experiments/2026-10-20-repetition-validation/RESULTS.md) | Expanded generation quality | 108 outputs; exact ASR47/54 each, ending-group regression retained | No quality/adoption claim |
| [2026-10-20-stream-guard](experiments/2026-10-20-stream-guard/RESULTS.md) | Tail buffering and model size | 104 model outputs; quiet-ending crop counterexamples | Keep0.6B; guard disabled |
| [2026-10-20-tail-safety](experiments/2026-10-20-tail-safety/RESULTS.md) | Conservative tail safety | Digital-zero rule preserves nonzero samples but has no real-tail benefit | Do not deploy either crop rule |
| [2026-10-20-tts-contention](experiments/2026-10-20-tts-contention/RESULTS.md) | Shared-GPU contention | 60 trials: concurrent decode raises TTS median; idle outlier persists | Diagnose contention without general latency guarantee |
| [2026-10-20-tts-tail](experiments/2026-10-20-tts-tail/RESULTS.md) | Generated silence | 40-seed isolated trace reproduces pre/post speech silence | Investigate generation; offline crop is not deployment |
| [2026-10-20-xvector-integration](experiments/2026-10-20-xvector-integration/RESULTS.md) | Vector-only integration | 12 cancellation +8 queue +18DSH; first clauses differ | Candidate only; no causal general speedup |
| [2026-10-20-xvector-listening](experiments/2026-10-20-xvector-listening/RESULTS.md) | Blind conditioning comparison | 32 randomized pairs; rating export not yet imported | Human acceptance pending |
| [2026-10-21-cancel-recovery](experiments/2026-10-21-cancel-recovery/RESULTS.md) | Stress and recovery | 36HTTP,8 programmed stops; queue peak8 but slow sender holds lock longer | Next: sender deadline and whole-request lock budget |

## Compare like with like

Complete chain: last voiced synthetic input → scheduled browser first voiced output. TTS-only: request/handler → first voiced PCM. Cancellation: consumer close → actual generator exit. Playback finish and raw first chunk are separate endpoints. No endpoint includes physical audio hardware unless explicitly measured.

An automated contract pass does not establish human speech quality, native full-duplex operation or production acceptance. Raw recordings and runtime logs are local-only; numerical results and scripts remain public.
