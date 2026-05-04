.PHONY: setup check notebook bootstrap-data phase1 phase1-auto phase2 pca-analysis pca-full-compare data-prep-xgb eda-attack-drift build-hybrid train-hybrid train-ablation recall-recovery train-multiclass build-recall-splits train-high-recall stream-producer stream-consumer spark-stream-consumer campaign-aggregator api run-stack tmux-up tmux-down benchmark-baseline benchmark-full-plan head-to-head

PYTHON := .venv/bin/python3
JUPYTER := .venv/bin/jupyter

setup:
	./scripts/setup_mac.sh

check:
	$(PYTHON) scripts/check_environment.py

notebook:
	$(JUPYTER) notebook

phase1:
	$(PYTHON) scripts/phase1_prepare_pilot.py

bootstrap-data:
	$(PYTHON) scripts/bootstrap_dataset.py

phase1-auto: bootstrap-data phase1

phase2:
	$(PYTHON) scripts/phase2_train_baseline.py

pca-analysis:
	$(PYTHON) scripts/pca_interactive_analysis.py

pca-full-compare:
	$(PYTHON) scripts/pca_full_comparison.py

data-prep-xgb:
	$(PYTHON) scripts/data_analysis_preprocess_xgb.py

eda-attack-drift:
	$(PYTHON) scripts/eda_attack_drift_analysis.py

build-hybrid:
	$(PYTHON) scripts/build_hybrid_feature_space.py

train-hybrid:
	$(PYTHON) scripts/train_hybrid_xgb_stage.py

train-ablation:
	$(PYTHON) scripts/train_feature_set_ablation.py

recall-recovery:
	$(PYTHON) scripts/recall_recovery_experiments.py

train-multiclass:
	$(PYTHON) scripts/train_multiclass_hybrid_xgb.py

build-recall-splits:
	$(PYTHON) scripts/build_recall_splits.py

train-high-recall:
	$(PYTHON) scripts/train_high_recall_xgb.py

stream-producer:
	$(PYTHON) scripts/stream_kafka_producer.py

stream-consumer:
	$(PYTHON) scripts/stream_kafka_inference_consumer.py

spark-stream-consumer:
	$(PYTHON) scripts/spark_kafka_inference_consumer_v3.py

campaign-aggregator:
	$(PYTHON) scripts/stream_campaign_aggregator.py

api:
	.venv/bin/uvicorn scripts.api_server:app --host 0.0.0.0 --port 8000 --reload

run-stack:
	@echo "Start these in separate terminals:"
	@echo "1) make stream-consumer"
	@echo "2) make campaign-aggregator"
	@echo "3) make api"
	@echo "4) make stream-producer"

tmux-up:
	./scripts/start_stack_tmux.sh

tmux-down:
	./scripts/stop_stack_tmux.sh

benchmark-baseline:
	$(PYTHON) scripts/benchmark_baseline.py

benchmark-full-plan:
	$(PYTHON) scripts/generate_full_benchmark_plan.py

head-to-head:
	$(PYTHON) scripts/run_head_to_head.py --rows 1000 --timeout-sec 120 --heartbeat-sec 5
