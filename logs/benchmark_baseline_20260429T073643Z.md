# Baseline Benchmark Report

- Generated at: `2026-04-29T07:37:21.869164+00:00`
- Host: `macOS-local`

## Profiles

| Profile | Rows | Producer EPS | Consumer EPS | P95 Ingest->Write (ms) | Mongo Errors |
|---|---:|---:|---:|---:|---:|
| low | 2000 | 209.5 | 0.0 | 0.0 | 0 |
| medium | 5000 | 475.9 | 0.0 | 0.0 | 0 |
| high_local_safe | 10000 | 1101.5 | 0.0 | 0.0 | 0 |

## Bottleneck Hint

Consumer/model inference is likely bottleneck (producer EPS exceeds consumer EPS).
