# Baseline Benchmark Report

- Generated at: `2026-04-29T07:15:23.806815+00:00`
- Host: `macOS-local`

## Profiles

| Profile | Rows | Producer EPS | Consumer EPS | P95 Ingest->Write (ms) | Mongo Errors |
|---|---:|---:|---:|---:|---:|
| low | 2000 | 267.2 | 10.3 | 294540.3 | 0 |
| medium | 5000 | 539.9 | 10.3 | 294540.3 | 0 |
| high_local_safe | 10000 | 1437.4 | 10.3 | 294540.3 | 0 |

## Bottleneck Hint

Consumer/model inference is likely bottleneck (producer EPS exceeds consumer EPS).
