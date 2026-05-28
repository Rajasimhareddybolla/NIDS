# Baseline Benchmark Report

- Generated at: `2026-04-29T08:01:01.574953+00:00`
- Host: `macOS-local`

## Profiles

| Profile | Rows | Producer EPS | Consumer EPS | P95 Ingest->Write (ms) | Mongo Errors |
|---|---:|---:|---:|---:|---:|
| low | 2000 | 251.6 | 147.9 | 2165.9 | 0 |
| medium | 5000 | 441.9 | 126.1 | 1832.2 | 0 |
| high_local_safe | 10000 | 733.3 | 675.4 | 8081.2 | 0 |

## Bottleneck Hint

Consumer/model inference is likely bottleneck (producer EPS exceeds consumer EPS).
