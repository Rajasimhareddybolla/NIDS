# Baseline Benchmark Report

- Generated at: `2026-04-29T07:55:35.904498+00:00`
- Host: `macOS-local`

## Profiles

| Profile | Rows | Producer EPS | Consumer EPS | P95 Ingest->Write (ms) | Mongo Errors |
|---|---:|---:|---:|---:|---:|
| low | 2000 | 261.5 | 85.0 | 2953.4 | 0 |
| medium | 5000 | 454.5 | 147.2 | 2496.3 | 0 |
| high_local_safe | 10000 | 1121.1 | 260.0 | 4842.2 | 0 |

## Bottleneck Hint

Consumer/model inference is likely bottleneck (producer EPS exceeds consumer EPS).
