# Full Benchmark Suite Plan

- Generated at: `2026-04-29T05:08:37.855482+00:00`
- Baseline local max EPS input: `0.00`

## Test Matrix

- Sustained ramp: 4 steps x 10m
- Burst spike: 30s bursts + 120s recovery windows
- Soak: 60m at 70% local stable ceiling
- Recovery: restart tests for consumer/mongo/api

## SLO Targets

- End-to-end latency p95 <= 5s
- Sustained throughput >= 10,000 EPS
- Peak spike handling >= 100,000 EPS
- Mongo write rate <= 500 docs/s