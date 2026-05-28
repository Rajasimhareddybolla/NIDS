# Baseline Benchmark Report

- Generated at: `2026-04-29T07:32:49.478805+00:00`
- Host: `macOS-local`

## Profiles

| Profile | Rows | Producer EPS | Consumer EPS | P95 Ingest->Write (ms) | Mongo Errors |
|---|---:|---:|---:|---:|---:|
| low | 2000 | 187.3 | 0.0 | 0.0 | 0 |
| medium | 5000 | 459.1 | 0.0 | 0.0 | 0 |
| high_local_safe | 10000 | 1173.0 | 0.0 | 0.0 | 0 |

## Bottleneck Hint

Consumer/model inference is likely bottleneck (producer EPS exceeds consumer EPS).
