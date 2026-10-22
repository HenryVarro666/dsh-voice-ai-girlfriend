# Archived Chinese turn evaluation

Source: 2026-10-19; numeric export: 2026-10-20.
No model inference is performed; all decisions use score > threshold.

## Label counts

| Dataset | Complete | Incomplete / internal pause cuts |
|---|---:|---:|
| zho | 473 | 456 |
| fleurs | 409 | 224 |
| td3k | 1133 | 1411 |

## Classifier metrics

TPR: complete accepted; FPR: incomplete accepted. Pauses are cut-level here.

| Dataset | Judge | Threshold | AUC | TPR | FPR | Accuracy |
|---|---|---:|---:|---:|---:|---:|
| zho | Qwen3-0.6B-end | 0.2 | 0.912 | 27.1% | 1.5% | 62.11% |
| zho | large | 0.99 | 0.895 | 40.6% | 3.3% | 68.14% |
| zho | rt | 0.99 | 0.867 | 33.4% | 4.2% | 64.05% |
| zho | zh | 0.99 | 0.869 | 36.2% | 4.2% | 65.45% |
| zho | smart-turn-fp32 | 0.5 | 0.964 | 91.8% | 10.7% | 90.53% |
| fleurs | Qwen3-0.6B-end | 0.2 | 0.807 | 44.5% | 13.4% | 59.40% |
| fleurs | large | 0.99 | 0.802 | 52.8% | 14.3% | 64.45% |
| fleurs | rt | 0.99 | 0.690 | 45.0% | 23.7% | 56.08% |
| fleurs | zh | 0.99 | 0.753 | 38.1% | 13.8% | 55.13% |
| fleurs | smart-turn-fp32 | 0.5 | 0.573 | 98.0% | 95.1% | 65.09% |
| td3k | Qwen3-0.6B-end | 0.2 | 0.954 | 46.9% | 1.8% | 75.35% |
| td3k | large | 0.99 | 0.903 | 19.3% | 0.4% | 63.88% |
| td3k | rt | 0.99 | 0.885 | 38.0% | 2.5% | 70.99% |
| td3k | zh | 0.99 | 0.857 | 31.1% | 1.9% | 68.24% |

## FLEURS timeout policy simulation

Wait excludes STT, scoring, network and playback time.
Cut rate uses all 409 complete source utterances as denominator, including
ones with no archived internal pause. This is not a conversation benchmark.

| Policy | Mean endpoint wait | Source utterances cut |
|---|---:|---:|
| Fixed 1.8 s | 1.80 s | 1.0% |
| Fixed 1.2 s | 1.20 s | 3.4% |
| Fixed 1.0 s | 1.00 s | 6.6% |
| Qwen3-0.6B-end, check 0.5 s / fallback 1.2 s | 0.89 s | 9.5% |
