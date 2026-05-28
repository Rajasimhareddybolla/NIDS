# Baseline Benchmark Report

- Generated at: `2026-04-29T07:18:23.361668+00:00`
- Host: `macOS-local`

## Profiles

| Profile | Rows | Producer EPS | Consumer EPS | P95 Ingest->Write (ms) | Mongo Errors |
|---|---:|---:|---:|---:|---:|
| low | 2000 | 225.1 | 13.0 | 278945.0 | 0 |
| medium | 5000 | 478.0 | 13.0 | 278945.0 | 0 |
| high_local_safe | 10000 | 1402.2 | 13.0 | 278945.0 | 0 |

## Bottleneck Hint

Consumer/model inference is likely bottleneck (producer EPS exceeds consumer EPS).
