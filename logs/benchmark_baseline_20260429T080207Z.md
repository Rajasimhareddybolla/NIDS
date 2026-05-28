# Baseline Benchmark Report

- Generated at: `2026-04-29T08:02:42.852619+00:00`
- Host: `macOS-local`

## Profiles

| Profile | Rows | Producer EPS | Consumer EPS | P95 Ingest->Write (ms) | Mongo Errors |
|---|---:|---:|---:|---:|---:|
| low | 2000 | 232.2 | 137.4 | 0.0 | 0 |
| medium | 5000 | 529.2 | 137.4 | 0.0 | 0 |
| high_local_safe | 10000 | 1455.8 | 137.4 | 0.0 | 0 |

## Bottleneck Hint

Consumer/model inference is likely bottleneck (producer EPS exceeds consumer EPS).
