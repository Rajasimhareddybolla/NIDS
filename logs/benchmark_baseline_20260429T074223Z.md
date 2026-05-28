# Baseline Benchmark Report

- Generated at: `2026-04-29T07:43:03.967129+00:00`
- Host: `macOS-local`

## Profiles

| Profile | Rows | Producer EPS | Consumer EPS | P95 Ingest->Write (ms) | Mongo Errors |
|---|---:|---:|---:|---:|---:|
| low | 2000 | 166.7 | 0.0 | 0.0 | 0 |
| medium | 5000 | 511.0 | 0.0 | 0.0 | 0 |
| high_local_safe | 10000 | 1032.4 | 0.0 | 0.0 | 0 |

## Bottleneck Hint

Consumer/model inference is likely bottleneck (producer EPS exceeds consumer EPS).
