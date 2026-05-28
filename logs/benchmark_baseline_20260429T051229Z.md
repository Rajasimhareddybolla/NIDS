# Baseline Benchmark Report

- Generated at: `2026-04-29T05:13:07.971876+00:00`
- Host: `macOS-local`

## Profiles

| Profile | Rows | Producer EPS | Consumer EPS | P95 Ingest->Write (ms) | Mongo Errors |
|---|---:|---:|---:|---:|---:|
| low | 2000 | 234.2 | 403.0 | 0.0 | 0 |
| medium | 5000 | 460.0 | 439.6 | 0.0 | 0 |
| high_local_safe | 10000 | 1048.1 | 454.1 | 0.0 | 0 |

## Bottleneck Hint

Consumer/model inference is likely bottleneck (producer EPS exceeds consumer EPS).
