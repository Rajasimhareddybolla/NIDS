# Baseline Benchmark Report

- Generated at: `2026-04-29T07:40:07.423857+00:00`
- Host: `macOS-local`

## Profiles

| Profile | Rows | Producer EPS | Consumer EPS | P95 Ingest->Write (ms) | Mongo Errors |
|---|---:|---:|---:|---:|---:|
| low | 2000 | 176.1 | 0.0 | 0.0 | 0 |
| medium | 5000 | 514.6 | 0.0 | 0.0 | 0 |
| high_local_safe | 10000 | 1061.2 | 0.0 | 0.0 | 0 |

## Bottleneck Hint

Consumer/model inference is likely bottleneck (producer EPS exceeds consumer EPS).
