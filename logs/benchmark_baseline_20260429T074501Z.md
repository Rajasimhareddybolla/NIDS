# Baseline Benchmark Report

- Generated at: `2026-04-29T07:45:36.082016+00:00`
- Host: `macOS-local`

## Profiles

| Profile | Rows | Producer EPS | Consumer EPS | P95 Ingest->Write (ms) | Mongo Errors |
|---|---:|---:|---:|---:|---:|
| low | 2000 | 236.8 | 0.0 | 0.0 | 0 |
| medium | 5000 | 539.6 | 0.0 | 0.0 | 0 |
| high_local_safe | 10000 | 1436.2 | 0.0 | 0.0 | 0 |

## Bottleneck Hint

Consumer/model inference is likely bottleneck (producer EPS exceeds consumer EPS).
