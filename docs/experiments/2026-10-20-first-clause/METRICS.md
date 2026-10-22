# First-clause replay metrics

Time zero is the first content packet of a recorded copy task. No ASR, DSH prefill or real browser playback.

| Policy | n | Text ready median | First voiced PCM median | Ideal first voice estimate | Full synthesis median | Ideal starvation median |
|---|---:|---:|---:|---:|---:|---:|
| current_5 | 12 | 41.2 ms | 225.1 ms | 249.5 ms | 2717.9 ms | 0.0 ms |
| later_18 | 12 | 93.6 ms | 286.4 ms | 326.4 ms | 2788.3 ms | 0.0 ms |
| sentence | 12 | 135.7 ms | 237.9 ms | 277.9 ms | 2586.0 ms | 0.0 ms |

## First voiced PCM medians by fixed answer

| Text ID | current_5 | later_18 | sentence |
|---|---:|---:|---:|
| 0 | 226.6 ms | 265.2 ms | 228.6 ms |
| 1 | 225.2 ms | 187.8 ms | 234.4 ms |
| 2 | 150.4 ms | 371.5 ms | 324.8 ms |
| 3 | 225.0 ms | 286.4 ms | 324.3 ms |
