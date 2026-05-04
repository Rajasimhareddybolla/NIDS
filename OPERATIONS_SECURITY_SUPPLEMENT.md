# NIDS Operations and Security Supplement

This supplement captures important implementation and operational details that are not fully covered in:
- `NIDS_Design_Document_v3.docx`
- `IMPLEMENTATION_EXECUTION_REPORT.md`
- `EXPERIMENT_RESULTS_REPORT.md`

It focuses on practical execution reliability, security posture, secret handling, failure recovery, and real-world caveats.

---

## 1) Non-Obvious Implementation Facts

- The current streaming inference uses `models/xgb_only_targeted10.joblib` by default in `stream_kafka_inference_consumer.py`.
- The best offline model from ablation is `models/xgb_combo_targeted10_pca25.joblib`; this is not yet the default streaming model path unless `STREAM_MODEL_PATH` is overridden.
- Current simulation dataset variant does not include real source/destination IP columns; producer synthesizes deterministic IPs when missing.
- API is designed to degrade gracefully when Mongo is unavailable (returns empty lists vs hard failure on list endpoints).
- Campaign aggregation is implemented as a polling/upsert worker (not Spark state store); it is practical for local demo/validation but not equivalent to true distributed CEP guarantees.

---

## 2) Secrets and Sensitive Configuration

## 2.1 Where secrets/config live

- `.env` (local runtime config)
- `.env.example` (template)
- Environment variables at process runtime

Current sensitive-ish fields:
- `MONGODB_URI`
- `KAFKA_BOOTSTRAP_SERVERS` (infra endpoint)
- any future credentials/tokens if added

## 2.2 Secret handling policy for this project

- Do **not** commit `.env` to git.
- Keep `.env.example` non-sensitive (placeholders/defaults only).
- Do not hardcode credentials in scripts.
- Avoid logging full connection strings in production logs.
- Rotate credentials when moving from local demo to shared infrastructure.

## 2.3 What is not stored in this repo

- No API keys are embedded in code.
- No cloud credentials are committed.
- No Mongo auth password is hardcoded in scripts.

---

## 3) Security and Privacy Caveats

- Synthetic IP generation is for simulation visibility only; do not treat as forensic truth.
- Current API has no auth/rate-limit; suitable for local/demo only.
- No TLS configured for API/Kafka/Mongo in this local setup.
- No RBAC on data access.
- No audit-trail signing for incident events.

Recommended before production:
- API authentication/authorization
- TLS in transit
- secret manager integration
- network segmentation and IP allow-listing
- immutable audit logging

---

## 4) Operational Runbook (Failure-Oriented)

## 4.1 Service dependency order

1. Kafka
2. MongoDB
3. stream-consumer
4. campaign-aggregator
5. API
6. stream-producer

## 4.2 Common failures and fixes

- `NoBrokersAvailable`:
  - Kafka not running or wrong broker endpoint.
- `MongoNetworkError ECONNREFUSED`:
  - MongoDB service down, wrong URI, or startup delay.
- `ModuleNotFoundError: kafka`:
  - command ran outside `.venv`; fixed via Makefile `PYTHON := .venv/bin/python3`.
- API port in use:
  - another `uvicorn` instance already bound to port `8000`.

## 4.3 Safe restart sequence

- Stop producer first (if running heavy stream)
- Stop consumer
- Stop aggregator
- Restart infra (Kafka/Mongo)
- Restart consumer -> aggregator -> API -> producer

---

## 5) Data Quality and Semantics Caveats

- Label normalization is necessary (`Web Attack` variants, unicode artifact normalization).
- Day-split strategy can create unseen classes in validation (multiclass gate failure expected under strict Mon-Thu train / Fri validation).
- Duplicate flows are present in raw dataset and are removed during preprocessing steps.
- Class imbalance is severe; aggregate accuracy is not a trustworthy headline metric.

---

## 6) Model Governance Notes

- Keep track of:
  - model artifact path
  - feature set contract
  - threshold used at runtime
  - data split policy used for the reported metric
- Current dashboard metric cards are tied to latest logged outputs, so report provenance must be verified when rerunning experiments.
- Always pair metrics with run timestamp and artifact path.

---

## 7) Performance and Capacity Notes

- Streaming rates observed in local runs are workload- and hardware-dependent.
- Throughput endpoint reports persisted incident rate, not total Kafka input rate.
- End-to-end latency is not yet fully instrumented with ingest-to-persist percentile tracking for every run.

If stricter SLO reporting is needed, add:
- producer ingest timestamp (`ingest_ts`)
- consumer detection timestamp
- DB write timestamp
- p50/p95/p99 latency computation job

---

## 8) Monitoring Gaps (Known)

Not yet fully implemented:
- queue lag tracking (Kafka consumer lag)
- automated alerting thresholds
- long-term metric retention
- per-service structured log correlation IDs

Suggested minimum additions:
- lag endpoint
- write failures counter
- consumer heartbeat metric
- campaign upsert error metric

---

## 9) Productionization Checklist (Short)

- [ ] Replace synthetic IP fallback with real network identifiers
- [ ] Add API auth + role controls
- [ ] Enforce TLS for all external interfaces
- [ ] Externalize secrets to secret manager
- [ ] Add CI tests for scripts and API contracts
- [ ] Add canary/backfill strategy for model updates
- [ ] Add rollback procedure for model/threshold changes

---

## 10) Practical “Do/Don’t”

Do:
- run via `make` targets to ensure `.venv` usage
- pin execution to known model artifact and threshold when validating
- log run timestamps and output files

Don’t:
- copy local demo metrics as production guarantees
- expose unauthenticated dashboard publicly
- treat synthetic IP values as true source attribution

---

## 11) Benchmark and SLO Governance

- Baseline benchmark automation now exists via `make benchmark-baseline`.
- Runtime benchmark snapshot is written by consumer to `logs/benchmark_runtime_metrics.json`.
- SLO evaluation endpoint is available at `GET /metrics/slo` with statuses:
  - `PASS`
  - `FAIL`
  - `UNVERIFIED`
- Full-suite benchmark matrix generation is available via `make benchmark-full-plan`.

Current SLO target set encoded:
- End-to-end latency p95 <= 5 seconds
- Sustained throughput >= 10,000 EPS
- Peak spike handling >= 100,000 EPS
- Mongo write rate <= 500 docs/s

Notes:
- Throughput and spike status may remain `UNVERIFIED` unless benchmark runs supply evidence.
- Local Mac ceiling should be treated as practical operating target; design targets are retained for architecture validation.

