# Head-to-Head Benchmark (High-Recall Model)

## Scope

This report compares the two streaming inference consumers on the same setup after promoting the new high-recall model:

- Python consumer: `scripts/stream_kafka_inference_consumer.py`
- Spark consumer: `scripts/spark_kafka_inference_consumer_v3.py`
- Promoted model: `models/xgb_high_recall_best.joblib`
- Feature space: `raw_numeric`
- Threshold: `0.01`
- Input size: `10,000` events per case
- Benchmark runner: `scripts/run_head_to_head.py`

Primary result source:
- `logs/head_to_head_results.json`

Supplemental logs:
- `logs/head_to_head_python_consumer.log`
- `logs/head_to_head_spark_v3_consumer.log`

---

## Test Configuration

- Kafka topics were isolated per case (one for Python, one for Spark).
- Producer settings:
  - `STREAM_MAX_ROWS=10000`
  - `STREAM_SLEEP_MS=0`
- Consumer settings:
  - Python: `STREAM_CONSUME_MAX_MESSAGES=10000`, `KAFKA_AUTO_OFFSET_RESET=earliest`
  - Spark: `SPARK_STARTING_OFFSETS=earliest`
- Spark checkpoint path was reset for the Spark case.
- Runtime metrics were read from `logs/benchmark_runtime_metrics.json`.

---

## Quantitative Comparison

| Metric | Python consumer | Spark v3 consumer |
|---|---:|---:|
| Rows produced | 10,000 | 10,000 |
| Producer elapsed (s) | 6.027 | 6.434 |
| Producer EPS | 1,659.27 | 1,554.16 |
| Processed target reached (runner) | false* | true |
| Processed total (runner snapshot) | 9,000* | 10,000 |
| Final processed rate EPS | 281.63 | 836.59 |
| p95 ingest->write latency (ms) | 24,429.11 | 5,848.69 |
| p95 detect->write latency (ms) | 2.91 | 680.50 |
| Final consumer lag (messages) | 1,000* | 0 |
| Active batch EPS (Spark internal) | N/A | 2,846.38 |

\* Python caveat: consumer log confirms completion to 10,000 (`Finished: processed=10000 threats=50`), but the runner captured a stale final metrics snapshot at 9,000 due to timing of metric-file updates versus process exit.

---

## Qualitative Interpretation

### 1) Throughput winner

Spark v3 is clearly faster in end-to-end processing rate for this run:
- Spark: ~836.6 EPS
- Python: ~281.6 EPS

That is roughly **2.97x higher** processed EPS for Spark on this benchmark.

### 2) Latency behavior

- Spark has much better **ingest->write p95** than Python in this run (5.85s vs 24.43s), which aligns with Spark draining backlog faster.
- Python has much lower **detect->write p95** because each event write path is lightweight once a message is in-process.
- Spark’s detect->write includes micro-batch timing effects, so this metric is expected to be higher even when global throughput is better.

### 3) Backlog handling

- Spark ended with lag `0` and reached target.
- Python consumed all 10k per consumer log, but runner snapshot still showed lag `1000` at capture time; this is a measurement timing artifact, not a true processing failure.

### 4) Operational notes

Spark logs show recurring warnings that micro-batches sometimes exceeded the 1s trigger interval. This indicates occasional scheduling/processing pressure, but overall completion and throughput were still strong.

---

## Bottom-Line A/B Conclusion

For the current promoted high-recall model (`raw_numeric`, threshold `0.01`):

- **If the goal is max sustained throughput and queue-drain speed:** Spark v3 is the better choice.
- **If the goal is lower per-record detect->write path latency and simpler runtime:** Python remains operationally simpler but slower under this load.

Given this 10k benchmark, Spark v3 is the recommended default consumer for high-volume ingestion windows.

---

## Known Measurement Limitation

The runner currently reads the runtime metrics file at checkpoints; for fast exits, final Python state can be under-reported. The consumer log already contains the true terminal count and should be treated as source-of-truth for completion.

Potential improvement:
- In `scripts/run_head_to_head.py`, add a final metrics refresh after consumer exit (or parse the consumer log final line) before writing summary JSON.
