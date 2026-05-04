from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from kafka import KafkaConsumer
from pymongo import MongoClient


ROOT = Path(__file__).resolve().parent.parent
TARGETED_10 = [
    "Destination Port",
    "Flow Duration",
    "SYN Flag Count",
    "ACK Flag Count",
    "FIN Flag Count",
    "Total Fwd Packets",
    "Total Backward Packets",
    "Fwd Packet Length Max",
    "Bwd Packet Length Max",
    "Flow IAT Mean",
]


def to_float(v: Any) -> float:
    if v is None:
        return np.nan
    try:
        return float(v)
    except Exception:
        return np.nan


def pick(payload: dict[str, Any], keys: list[str]) -> Any:
    for k in keys:
        v = payload.get(k)
        if v is not None and str(v).strip() != "":
            return v
    return None


def load_threshold() -> float:
    p = ROOT / "config" / "dynamic_params.json"
    if not p.exists():
        return 0.3
    data = json.loads(p.read_text(encoding="utf-8"))
    return float(data.get("classification_threshold", 0.3))


def load_dynamic_params() -> dict[str, Any]:
    p = ROOT / "config" / "dynamic_params.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def append_benchmark_snapshot(snapshot_path: Path, data: dict[str, Any]) -> None:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data) + "\n")


def parse_iso_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def main() -> None:
    load_dotenv(ROOT / ".env")
    bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    topic = os.getenv("KAFKA_TOPIC", "network-telemetry")
    group_id = os.getenv("KAFKA_GROUP_ID", "nids-inference-consumer")
    auto_offset_reset = os.getenv("KAFKA_AUTO_OFFSET_RESET", "latest")
    dynamic = load_dynamic_params()
    model_path = Path(
        os.getenv(
            "STREAM_MODEL_PATH",
            str(dynamic.get("high_recall_model_path", ROOT / "models" / "xgb_combo_targeted10_pca25.joblib")),
        )
    )
    threshold = float(os.getenv("STREAM_THRESHOLD", str(load_threshold())))
    mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    mongo_db = os.getenv("MONGODB_DB", "nids")
    incidents_col = os.getenv("MONGODB_INCIDENTS_COLLECTION", "security_incidents")
    max_messages = int(os.getenv("STREAM_CONSUME_MAX_MESSAGES", "0"))
    metrics_snapshot = ROOT / "logs" / "benchmark_runtime_metrics.json"
    lag_history_path = ROOT / "logs" / "consumer_lag_history.jsonl"

    selected_feature_space = str(dynamic.get("high_recall_feature_space", "hybrid_10plus25"))
    preprocess_bundle_path = ROOT / "models" / "hybrid_preprocess_bundle.json"
    preprocess_dir = ROOT / "models"
    imputer_path = preprocess_dir / "hybrid_preprocess_imputer.joblib"
    scaler_path = preprocess_dir / "hybrid_preprocess_scaler.joblib"
    pca_path = preprocess_dir / "hybrid_preprocess_pca.joblib"

    if not model_path.exists():
        raise SystemExit(f"Model not found: {model_path}")
    model = joblib.load(model_path)
    targeted_10: list[str] = []
    pca_pool: list[str] = []
    pca_cols: list[str] = []
    imputer = None
    scaler = None
    pca = None
    feature_cols: list[str] = []
    if selected_feature_space == "hybrid_10plus25":
        if not preprocess_bundle_path.exists() or not imputer_path.exists() or not scaler_path.exists() or not pca_path.exists():
            raise SystemExit(
                "Hybrid preprocess artifacts missing. Run `make build-hybrid` first "
                "(it now saves hybrid_preprocess_*.joblib + bundle.json)."
            )
        preprocess_bundle = json.loads(preprocess_bundle_path.read_text(encoding="utf-8"))
        targeted_10 = preprocess_bundle["targeted_10"]
        pca_pool = preprocess_bundle["pca_pool"]
        pca_cols = preprocess_bundle["pca_cols"]
        imputer = joblib.load(imputer_path)
        scaler = joblib.load(scaler_path)
        pca = joblib.load(pca_path)
        feature_cols = targeted_10 + pca_cols
    elif selected_feature_space == "targeted10_only":
        feature_cols = list(getattr(model, "feature_names_in_", []))
        if not feature_cols:
            feature_cols = TARGETED_10
    else:
        feature_cols = list(getattr(model, "feature_names_in_", []))
        if not feature_cols:
            raise SystemExit("Raw numeric feature names are unavailable on the trained model.")

    mongo_client = None
    incidents = None
    try:
        mongo_client = MongoClient(mongo_uri, serverSelectionTimeoutMS=1500)
        mongo_client.admin.command("ping")
        incidents = mongo_client[mongo_db][incidents_col]
        print(f"Mongo connected: {mongo_uri}/{mongo_db}.{incidents_col}")
    except Exception:
        incidents = None
        print("Mongo not reachable. Running in log-only mode.")

    consumer = KafkaConsumer(
        topic,
        bootstrap_servers=[bootstrap],
        group_id=group_id,
        auto_offset_reset=auto_offset_reset,
        enable_auto_commit=True,
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
    )
    print(
        f"Consuming topic={topic} bootstrap={bootstrap} threshold={threshold} feature_space={selected_feature_space}"
    )

    total = 0
    threats = 0
    mongo_write_errors = 0
    mongo_write_success = 0
    latency_samples_ms: list[float] = []
    detect_to_write_samples_ms: list[float] = []
    process_latency_samples_ms: list[float] = []
    start = time.time()
    for msg in consumer:
        detect_dt = datetime.now(timezone.utc)
        payload = msg.value if isinstance(msg.value, dict) else {}
        if selected_feature_space == "hybrid_10plus25":
            x_targeted = np.array([[to_float(payload.get(f)) for f in targeted_10]], dtype=float)
            x_pca_pool = np.array([[to_float(payload.get(f)) for f in pca_pool]], dtype=float)
            if not np.isfinite(x_targeted).all():
                x_targeted[~np.isfinite(x_targeted)] = np.nan
            if not np.isfinite(x_pca_pool).all():
                x_pca_pool[~np.isfinite(x_pca_pool)] = np.nan
            x_pca_imp = imputer.transform(x_pca_pool)
            x_pca_scaled = scaler.transform(x_pca_imp)
            x_pca_vec = pca.transform(x_pca_scaled)
            x_input = np.hstack([x_targeted, x_pca_vec])
        else:
            x_input = pd.DataFrame(
                [{c: to_float(payload.get(c)) for c in feature_cols}],
                columns=feature_cols,
            )
        prob = float(model.predict_proba(x_input)[0, 1])
        is_threat = prob >= threshold
        total += 1

        if is_threat:
            threats += 1
            event = {
                "timestamp": detect_dt.isoformat(),
                "ingest_ts": payload.get("ingest_ts"),
                "detect_ts": detect_dt.isoformat(),
                "confidence": prob,
                "threshold": threshold,
                "threat_classification": "malicious",
                "benchmark": {
                    "run_id": payload.get("benchmark_run_id"),
                    "profile": payload.get("benchmark_profile"),
                },
                "network_identifiers": {
                    "source_ip": pick(payload, ["source_ip", "Source IP", "Src IP"]),
                    "destination_ip": pick(payload, ["destination_ip", "Destination IP", "Dst IP"]),
                    "target_port": pick(payload, ["Destination Port", "Dst Port", "target_port"]),
                },
                "features": {k: payload.get(k) for k in (targeted_10 if targeted_10 else feature_cols[:10])},
            }
            ingest_dt = parse_iso_ts(payload.get("ingest_ts"))
            if ingest_dt is not None:
                latency_samples_ms.append((detect_dt - ingest_dt).total_seconds() * 1000.0)
            if incidents is not None:
                try:
                    write_dt = datetime.now(timezone.utc)
                    event["mongo_write_ts"] = write_dt.isoformat()
                    if ingest_dt is not None:
                        event["latency_ingest_to_detect_ms"] = (
                            detect_dt - ingest_dt
                        ).total_seconds() * 1000.0
                        event["latency_detect_to_write_ms"] = (
                            write_dt - detect_dt
                        ).total_seconds() * 1000.0
                        event["latency_ingest_to_write_ms"] = (
                            write_dt - ingest_dt
                        ).total_seconds() * 1000.0
                        detect_to_write_samples_ms.append(event["latency_detect_to_write_ms"])
                        process_latency_samples_ms.append(event["latency_ingest_to_write_ms"])
                    incidents.insert_one(event)
                    mongo_write_success += 1
                except Exception:
                    mongo_write_errors += 1

        if total % 1000 == 0:
            elapsed = time.time() - start
            print(f"processed={total} threats={threats} rate={total/max(elapsed,1e-6):.1f}/s")
            lag_messages = 0
            try:
                partitions = list(consumer.assignment())
                if partitions:
                    ends = consumer.end_offsets(partitions)
                    for tp in partitions:
                        current_pos = consumer.position(tp)
                        lag_messages += max(0, int(ends.get(tp, 0)) - int(current_pos))
            except Exception:
                lag_messages = 0
            snapshot = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "processed_total": total,
                "threats_total": threats,
                "processed_rate_eps": total / max(elapsed, 1e-6),
                "mongo_write_success": mongo_write_success,
                "mongo_write_errors": mongo_write_errors,
                "consumer_lag_messages": lag_messages,
                "latency_ingest_to_detect_ms_avg": (
                    float(np.mean(latency_samples_ms)) if latency_samples_ms else 0.0
                ),
                "latency_ingest_to_detect_ms_p95": (
                    float(np.percentile(latency_samples_ms, 95))
                    if latency_samples_ms
                    else 0.0
                ),
                "latency_ingest_to_write_ms_avg": (
                    float(np.mean(process_latency_samples_ms)) if process_latency_samples_ms else 0.0
                ),
                "latency_ingest_to_write_ms_p95": (
                    float(np.percentile(process_latency_samples_ms, 95))
                    if process_latency_samples_ms
                    else 0.0
                ),
                "latency_ingest_to_write_ms_p99": (
                    float(np.percentile(process_latency_samples_ms, 99))
                    if process_latency_samples_ms
                    else 0.0
                ),
                "latency_detect_to_write_ms_avg": (
                    float(np.mean(detect_to_write_samples_ms))
                    if detect_to_write_samples_ms
                    else 0.0
                ),
                "latency_detect_to_write_ms_p95": (
                    float(np.percentile(detect_to_write_samples_ms, 95))
                    if detect_to_write_samples_ms
                    else 0.0
                ),
            }
            append_benchmark_snapshot(metrics_snapshot, snapshot)
            append_jsonl(
                lag_history_path,
                {
                    "t": snapshot["updated_at"],
                    "processed_total": total,
                    "consumer_lag_messages": lag_messages,
                    "processed_rate_eps": snapshot["processed_rate_eps"],
                },
            )

        if max_messages > 0 and total >= max_messages:
            break

    elapsed = time.time() - start
    final_snapshot = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "processed_total": total,
        "threats_total": threats,
        "processed_rate_eps": total / max(elapsed, 1e-6),
        "mongo_write_success": mongo_write_success,
        "mongo_write_errors": mongo_write_errors,
        "consumer_lag_messages": 0,
        "latency_ingest_to_detect_ms_avg": float(np.mean(latency_samples_ms)) if latency_samples_ms else 0.0,
        "latency_ingest_to_detect_ms_p95": float(np.percentile(latency_samples_ms, 95))
        if latency_samples_ms
        else 0.0,
        "latency_ingest_to_write_ms_avg": (
            float(np.mean(process_latency_samples_ms)) if process_latency_samples_ms else 0.0
        ),
        "latency_ingest_to_write_ms_p95": float(np.percentile(process_latency_samples_ms, 95))
        if process_latency_samples_ms
        else 0.0,
        "latency_detect_to_write_ms_avg": (
            float(np.mean(detect_to_write_samples_ms)) if detect_to_write_samples_ms else 0.0
        ),
        "latency_detect_to_write_ms_p95": float(np.percentile(detect_to_write_samples_ms, 95))
        if detect_to_write_samples_ms
        else 0.0,
    }
    append_benchmark_snapshot(metrics_snapshot, final_snapshot)
    print(f"Finished: processed={total} threats={threats}")


if __name__ == "__main__":
    main()
