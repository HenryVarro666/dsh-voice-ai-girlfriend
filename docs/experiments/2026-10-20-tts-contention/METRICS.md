# Numeric TTS contention results

Attempts: 60. Errors: 0. Invalid decode overlap: 0.
Warmups are excluded. Units measure HTTP receipt, not browser playback or voice-to-voice latency.

| Condition | n | Median first voiced PCM | Mean | Min | Max | >1 s | Median RTF |
|---|---:|---:|---:|---:|---:|---:|---:|
| unloaded | 20 | 184.7 ms | 151.9 ms | 100.1 ms | 269.7 ms | 0 | 0.279 |
| resident | 20 | 185.2 ms | 254.6 ms | 101.1 ms | 1906.1 ms | 1 | 0.281 |
| decode | 20 | 313.1 ms | 289.0 ms | 187.6 ms | 439.5 ms | 0 | 0.411 |

## Median first voiced PCM by text

Text IDs refer to manifest.json; each cell uses the valid repetitions for that text and condition.

| Text ID | LLM absent | Resident | Decoding | Decoding minus resident |
|---|---:|---:|---:|---:|
| 0 | 185.2 ms | 185.2 ms | 430.7 ms | +245.5 ms |
| 1 | 101.8 ms | 101.8 ms | 313.1 ms | +211.2 ms |
| 2 | 184.8 ms | 185.3 ms | 313.2 ms | +127.9 ms |
| 3 | 184.9 ms | 185.8 ms | 196.5 ms | +10.7 ms |

No stable p95 claim. Four texts and stochastic synthesis limit generalization.
