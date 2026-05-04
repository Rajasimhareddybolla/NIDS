# Benchmark and SLO Runbook

## 1) Quick baseline (phase 1)

Prerequisites:
- `make tmux-up`
- ensure `consumer`, `api`, and `mongo` are reachable

Run:

```bash
make benchmark-baseline
```

Artifacts:
- `logs/benchmark_baseline_<timestamp>.json`
- `logs/benchmark_baseline_<timestamp>.md`
- `logs/benchmark_runtime_metrics.json` (written by consumer when restarted with latest code)

Live endpoints:
- `GET /metrics/benchmark`
- `GET /metrics/slo`

## 2) Full-suite planning (phase 2)

Generate the matrix:

```bash
make benchmark-full-plan
```

Artifacts:
- `logs/benchmark_full_plan_<timestamp>.json`
- `logs/benchmark_full_plan_<timestamp>.md`

Config references:
- `config/benchmark_profiles.yaml`
- `config/prometheus.yml`

## 3) Observability stack (recommended)

- Prometheus + Grafana for time-series dashboards
- Kafka JMX exporter for broker and consumer-lag visibility
- MongoDB exporter for write throughput, errors, and connection pressure
- Spark metrics/JVM metrics for micro-batch duration, scheduling delay, GC, and heap

## 4) Industry metrics checklist

- Latency: p50/p95/p99 ingest->detect->write
- Throughput: ingress, processed, persisted events/sec
- Reliability: error rates, retries, lag, recovery time
- Resource: CPU, memory, swap, GC, process RSS
- Data quality under load: confidence drift and detection-rate drift

## 5) SLO status semantics

- `PASS`: measured and within target
- `FAIL`: measured and outside target
- `UNVERIFIED`: no valid evidence captured yet

## 6) Current recommendation

- Use baseline outputs to establish local stable ceiling first.
- Attempt design targets after local ceiling is known.
- Treat local Mac results as engineering guidance, not production guarantees.
