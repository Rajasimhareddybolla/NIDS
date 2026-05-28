# Baseline Benchmark Report

- Generated at: `2026-04-29T07:19:56.209066+00:00`
- Host: `macOS-local`

## Profiles

| Profile | Rows | Producer EPS | Consumer EPS | P95 Ingest->Write (ms) | Mongo Errors |
|---|---:|---:|---:|---:|---:|
| low | 2000 | 224.0 | 13.0 | 278945.0 | 0 |
| medium | 5000 | 479.5 | 13.0 | 278945.0 | 0 |
| high_local_safe | 10000 | 1394.3 | 13.0 | 278945.0 | 0 |

## Bottleneck Hint

Consumer/model inference is likely bottleneck (producer EPS exceeds consumer EPS).
