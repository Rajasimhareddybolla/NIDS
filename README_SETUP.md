# NIDS Local Setup (macOS)

## 1) Run setup

```bash
cd /Users/raja/Desktop/bd
make setup
```

## 2) Activate environment

```bash
source .venv/bin/activate
```

## 3) Validate install

```bash
make check
```

This creates `logs/environment_report.json` with installed module/binary status.

## 4) Start notebook

```bash
make notebook
```

Use kernel: `Python (nids-local)`.

## 5) Run Phase 1 pilot preprocessing (10k rows)

```bash
make phase1
```

Outputs:
- Pilot CSV: `data/processed/pilot_10k.csv`
- Data quality log: `logs/phase1_data_quality_<timestamp>.json`

## 6) Run Phase 2 baseline training (dynamic params)

```bash
make phase2
```

Outputs:
- Model: `models/phase2_xgb_pipeline.joblib`
- Eval report: `logs/phase2_eval_<timestamp>.json`
- Updated dynamic params: `config/dynamic_params.json`

## 7) Interactive PCA complete analysis

```bash
make pca-analysis
```

Outputs:
- Interactive HTML report: `logs/pca_interactive_report_<timestamp>.html`
- PCA summary JSON: `logs/pca_summary_<timestamp>.json`

## 8) Full PCA comparison (fast vs balanced vs full)

```bash
make pca-full-compare
```

Outputs:
- JSON comparison: `logs/pca_full_comparison_<timestamp>.json`
- HTML comparison dashboard: `logs/pca_full_comparison_<timestamp>.html`

## 9) Data analysis + XGBoost-native preprocessing

```bash
make data-prep-xgb
```

Outputs:
- Train artifact: `data/processed/train_xgb_ready.pkl`
- Validation artifact: `data/processed/validation_xgb_ready.pkl`
- Analysis/preprocessing report: `logs/data_analysis_preprocess_xgb_<timestamp>.json`
- `config/dynamic_params.json` updated with `pca_k = 25`

## 10) Attack-class EDA + drift + correlation analysis

```bash
make eda-attack-drift
```

Output:
- `logs/eda_attack_drift_<timestamp>.json`

## 11) Build final hybrid feature space (10 + PCA25)

```bash
make build-hybrid
```

Outputs:
- `data/processed/train_hybrid_10plus25.pkl`
- `data/processed/validation_hybrid_10plus25.pkl`
- `logs/hybrid_feature_space_<timestamp>.json`

## 12) Train/evaluate hybrid XGBoost stage

```bash
make train-hybrid
```

Outputs:
- `models/hybrid_xgb_10plus25.joblib`
- `logs/hybrid_xgb_eval_<timestamp>.json`
- `config/dynamic_params.json` updated (`scale_pos_weight`, `classification_threshold`, `validation_f1_attack_binary`)

## 13) Train ablation: only10 vs onlyPCA25 vs combo

```bash
make train-ablation
```

Outputs:
- `logs/xgb_feature_set_ablation_<timestamp>.json`
- models:
  - `models/xgb_only_targeted10.joblib`
  - `models/xgb_only_pca25.joblib`
  - `models/xgb_combo_targeted10_pca25.joblib`

## 14) Multiclass recall-recovery training

```bash
make train-multiclass
```

Outputs:
- `models/multiclass_xgb_hybrid.joblib`
- `logs/multiclass_eval_<timestamp>.json`
- `logs/multiclass_vs_binary_<timestamp>.json`
- `config/dynamic_params.json` updated with multiclass operating metrics

## 15) Streaming stage (Kafka -> Inference -> Mongo)

Start consumer first:

Prereq (recommended):
```bash
make build-hybrid
```

```bash
make spark-stream-consumer
```

Then produce traffic rows:

```bash
make stream-producer
```

Optional env overrides:
- `STREAM_SOURCE_CSV` path to source CSV
- `STREAM_MAX_ROWS` max rows producer sends
- `STREAM_SLEEP_MS` delay between produced rows
- `STREAM_MODEL_PATH` model used by consumer (default: `models/xgb_combo_targeted10_pca25.joblib`)
- `STREAM_THRESHOLD` override threshold from `dynamic_params.json`
- `STREAM_CONSUME_MAX_MESSAGES` stop consumer after N messages

## 16) Campaign aggregation + Query API

Start campaign aggregator:

```bash
make campaign-aggregator
```

Start API server:

```bash
make api
```

Endpoints:
- `GET /health`
- `GET /incidents/recent?limit=100&minutes=60`
- `GET /campaigns/recent?limit=100&hours=24`
- `GET /campaigns/{attacker_ip}?limit=100`
- `GET /metrics/summary?hours=24`
- `GET /metrics/timeseries?minutes=180&bucket_minutes=5`
- `GET /metrics/throughput?minutes=5`
- `GET /metrics/model_eval`
- `GET /dashboard` (live visualization)

Helper command:

```bash
make run-stack
```

Single-command tmux stack:

```bash
make tmux-up
tmux attach -t nids-stack
```

Stop stack:

```bash
make tmux-down
```

## Benchmark and SLO validation

With stack running (`consumer`, `aggregator`, `api`), run:

```bash
make benchmark-baseline
```

Outputs:
- `logs/benchmark_baseline_<timestamp>.json`
- `logs/benchmark_baseline_<timestamp>.md`

Generate phase-2 full benchmark matrix:

```bash
make benchmark-full-plan
```

Outputs:
- `logs/benchmark_full_plan_<timestamp>.json`
- `logs/benchmark_full_plan_<timestamp>.md`

SLO and benchmark endpoints:
- `GET /metrics/benchmark`
- `GET /metrics/slo`
- `GET /metrics/consumer_lag`
- `GET /metrics/test_catalog`

Detailed benchmark guide:
- `BENCHMARK_SLO_RUNBOOK.md`
- `FASTAPI_SPARKML_ARCHITECTURE_GUIDE.md` (FastAPI design + Spark-ML architecture clarity)

Unified test dashboard:
- `GET /dashboard/tests` (all tests in one page: config, results, inference + consumer lag trend)

## Zero-manual data bootstrap (recommended)

If `data/raw` is empty, you can run:

```bash
make phase1-auto
```

This will:
- download CIC-IDS files via KaggleHub (`chethuhn/network-intrusion-dataset`)
- copy required CSVs into `data/raw`
- run Phase 1 preprocessing and logging

You can also run just dataset sync:

```bash
make bootstrap-data
```

## Notes

- `config/dynamic_params.json` is initialized with null values and should be filled by your training scripts.
- `config/dynamic_params.json` gets updated automatically after `make phase2`.
- `config/feature_contract.yaml` defines required columns and key feature families for preprocessing validation.
- `.env.example` contains default local values; copy to `.env` and update as needed.
- For your current machine (8GB RAM), start with a 10k-row pilot before running full-scale streaming.
