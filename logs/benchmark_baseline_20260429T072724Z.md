# Baseline Benchmark Report

- Generated at: `2026-04-29T07:28:02.368803+00:00`
- Host: `macOS-local`

## Profiles

| Profile | Rows | Producer EPS | Consumer EPS | P95 Ingest->Write (ms) | Mongo Errors |
|---|---:|---:|---:|---:|---:|
| low | 2000 | 241.0 | 13.0 | 278945.0 | 0 |
| medium | 5000 | 426.1 | 13.0 | 278945.0 | 0 |
| high_local_safe | 10000 | 1259.8 | 13.0 | 278945.0 | 0 |

## Bottleneck Hint

Consumer/model inference is likely bottleneck (producer EPS exceeds consumer EPS).
