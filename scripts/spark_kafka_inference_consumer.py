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
from kafka.structs import TopicPartition
from pymongo import MongoClient
from pyspark.sql import SparkSession
from pyspark.sql.functions import col


ROOT = Path(__file__).resolve().parent.parent


def to_float(v: Any) -> float:
    if v is None:
        return np.nan
    try:
        if isinstance(v, str) and v.strip() == "":
            return np.nan
        return float(v)
    except Exception:
        return np.nan


def pick(payload: dict[str, Any], keys: list[str]) -> Any:
    for k in keys:
        v = payload.get(k)
        if v is not None and str(v).strip() != "":
            return v
    return None


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
    mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    mongo_db = os.getenv("MONGODB_DB", "nids")
    incidents_col = os.getenv("MONGODB_INCIDENTS_COLLECTION", "security_incidents")

    group_id = os.getenv("KAFKA_GROUP_ID", "nids-inference-spark")
    model_path = Path(
        os.getenv(
            "STREAM_MODEL_PATH",
            str(ROOT / "models" / "xgb_combo_targeted10_pca25.joblib"),
        )
    )
    threshold_default = float(
        json.loads((ROOT / "config" / "dynamic_params.json").read_text(encoding="utf-8")).get(
            "classification_threshold", 0.3
        )
    ) if (ROOT / "config" / "dynamic_params.json").exists() else 0.3
    threshold = float(os.getenv("STREAM_THRESHOLD", str(threshold_default)))

    preprocess_bundle_path = ROOT / "models" / "hybrid_preprocess_bundle.json"
    preprocess_dir = ROOT / "models"
    imputer_path = preprocess_dir / "hybrid_preprocess_imputer.joblib"
    scaler_path = preprocess_dir / "hybrid_preprocess_scaler.joblib"
    pca_path = preprocess_dir / "hybrid_preprocess_pca.joblib"

    if not model_path.exists():
        raise SystemExit(f"Model not found: {model_path}")
    if not preprocess_bundle_path.exists() or not imputer_path.exists() or not scaler_path.exists() or not pca_path.exists():
        raise SystemExit(
            "Hybrid preprocess artifacts missing. Run `make build-hybrid` first "
            "(it now saves hybrid_preprocess_*.joblib + bundle.json)."
        )

    preprocess_bundle = json.loads(preprocess_bundle_path.read_text(encoding="utf-8"))
    targeted_10: list[str] = preprocess_bundle["targeted_10"]
    pca_pool: list[str] = preprocess_bundle["pca_pool"]
    pca_cols: list[str] = preprocess_bundle["pca_cols"]

    imputer = joblib.load(imputer_path)
    scaler = joblib.load(scaler_path)
    pca = joblib.load(pca_path)
    model = joblib.load(model_path)
    feature_cols = targeted_10 + pca_cols

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

    # Ensure Kafka source connector is available.
    # Spark 4.1.1 uses Scala 2.13; we load the matching kafka package at runtime.
    # This will download the jar if not already cached.
    spark_kafka_spark_version = os.getenv("SPARK_KAFKA_SPARK_VERSION", "4.1.1")
    spark_kafka_scala_version = os.getenv("SPARK_KAFKA_SCALA_VERSION", "2.13")
    spark_kafka_pkg = os.getenv(
        "SPARK_KAFKA_PACKAGE",
        f"org.apache.spark:spark-sql-kafka-0-10_{spark_kafka_scala_version}:{spark_kafka_spark_version}",
    )

    spark = (
        SparkSession.builder.appName("nids-spark-inference")
        .config("spark.jars.packages", spark_kafka_pkg)
        .config("spark.hadoop.fs.defaultFS", "file:///")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")

    # Kafka source: we rely on Spark for micro-batch triggering/backpressure.
    df = (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", bootstrap)
        .option("subscribe", topic)
        .option("startingOffsets", "latest")
        .load()
    )

    # Spark columns for each micro-batch: key, value(bytes), topic, partition, offset, timestamp, etc.
    # We parse value bytes -> JSON dicts inside foreachBatch.
    df_parsed = df.select(col("partition"), col("offset"), col("value").alias("value_bytes"))

    start = time.time()
    total_consumed = 0
    threats_total = 0
    mongo_write_errors = 0
    mongo_write_success = 0

    latency_ingest_to_detect_ms: list[float] = []
    latency_ingest_to_write_ms: list[float] = []
    latency_detect_to_write_ms: list[float] = []

    benchmark_metrics_path = ROOT / "logs" / "benchmark_runtime_metrics.json"
    lag_history_path = ROOT / "logs" / "consumer_lag_history.jsonl"

    def append_benchmark_snapshot(data: dict[str, Any]) -> None:
        benchmark_metrics_path.parent.mkdir(parents=True, exist_ok=True)
        benchmark_metrics_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def append_jsonl(data: dict[str, Any]) -> None:
        lag_history_path.parent.mkdir(parents=True, exist_ok=True)
        with lag_history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data) + "\n")

    def foreach_batch(batch_df, batch_id: int) -> None:
        nonlocal total_consumed, threats_total, mongo_write_errors, mongo_write_success
        nonlocal latency_ingest_to_detect_ms, latency_ingest_to_write_ms, latency_detect_to_write_ms

        # Collect micro-batch rows to driver for now (local demo correctness first).
        pdf = batch_df.toPandas()
        json_records: list[dict[str, Any]] = []
        parse_failures = 0
        raw_values = pdf["value_bytes"].tolist()
        for v in raw_values:
            try:
                # Spark may yield `bytes`, `bytearray`, or `memoryview` for Kafka binary values.
                if isinstance(v, memoryview):
                    v = v.tobytes()
                if isinstance(v, (bytes, bytearray)):
                    s = v.decode("utf-8")
                elif isinstance(v, str):
                    s = v
                else:
                    s = str(v)
                json_records.append(json.loads(s))
            except Exception:
                parse_failures += 1
                continue

        batch_count = len(json_records)
        if batch_count == 0:
            print(f"[spark] batch_id={batch_id} empty parsed batch (raw_rows={len(raw_values)}, parse_failures={parse_failures})")
            return

        if batch_id % 1 == 0:
            print(f"[spark] batch_id={batch_id} raw_rows={len(raw_values)} parsed_rows={batch_count} parse_failures={parse_failures}")

        total_consumed += batch_count

        detect_dt = datetime.now(timezone.utc)
        # For approximate lag: backlog = end_offset - (max_offset_in_batch + 1)
        # We compute per partition using kafka end offsets.
        partitions = sorted(set(int(p) for p in pdf["partition"].tolist()))
        max_offset_by_partition: dict[int, int] = {}
        for p, off in zip(pdf["partition"].tolist(), pdf["offset"].tolist()):
            max_offset_by_partition[int(p)] = max(max_offset_by_partition.get(int(p), -1), int(off))

        lag_messages = 0
        try:
            tmp_consumer = KafkaConsumer(bootstrap_servers=[bootstrap], enable_auto_commit=False)
            tps = [TopicPartition(topic, p) for p in partitions]
            end_offsets = tmp_consumer.end_offsets(tps)
            for p in partitions:
                tp = TopicPartition(topic, p)
                end_off = end_offsets.get(tp, 0)
                lag_messages += max(0, int(end_off) - (max_offset_by_partition.get(p, -1) + 1))
            tmp_consumer.close()
        except Exception:
            lag_messages = 0

        df_batch = pd.DataFrame(json_records)

        # Build targeted + PCA inputs
        x_targeted = np.array(
            [[to_float(df_batch.get(c, pd.Series([np.nan] * batch_count)).iloc[i]) for c in targeted_10] for i in range(batch_count)],
            dtype=float,
        )
        x_pca_pool = np.array(
            [[to_float(df_batch.get(c, pd.Series([np.nan] * batch_count)).iloc[i]) for c in pca_pool] for i in range(batch_count)],
            dtype=float,
        )
        # Match training-time semantics where inf/-inf were coerced to NaN.
        if not np.isfinite(x_targeted).all():
            x_targeted[~np.isfinite(x_targeted)] = np.nan
        if not np.isfinite(x_pca_pool).all():
            x_pca_pool[~np.isfinite(x_pca_pool)] = np.nan

        x_pca_imp = imputer.transform(x_pca_pool)
        x_pca_scaled = scaler.transform(x_pca_imp)
        x_pca = pca.transform(x_pca_scaled)
        x_hybrid = np.hstack([x_targeted, x_pca])

        probs = model.predict_proba(x_hybrid)[:, 1]
        is_threat = probs >= threshold

        threats_batch = int(is_threat.sum())
        threats_total += threats_batch

        events: list[dict[str, Any]] = []
        latency_detect_batch_ms: list[float] = []
        latency_ingest_batch_ms: list[float] = []
        latency_write_batch_ms: list[float] = []

        # Prepare payload->event for threats
        for i, threat in enumerate(is_threat):
            if not threat:
                continue
            payload = json_records[i]

            ingest_dt = parse_iso_ts(payload.get("ingest_ts"))
            if ingest_dt is not None:
                ingest_to_detect = (detect_dt - ingest_dt).total_seconds() * 1000.0
                latency_ingest_batch_ms.append(ingest_to_detect)

            event = {
                "timestamp": detect_dt.isoformat(),
                "ingest_ts": payload.get("ingest_ts"),
                "detect_ts": detect_dt.isoformat(),
                "confidence": float(probs[i]),
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
                "features": {k: payload.get(k) for k in targeted_10},
            }
            events.append(event)

        if incidents is not None and events:
            write_dt = datetime.now(timezone.utc)
            for e in events:
                e["mongo_write_ts"] = write_dt.isoformat()

            # detect->write latency is ~batch write time - detect time.
            latency_detect_batch_ms = [(write_dt - detect_dt).total_seconds() * 1000.0] * len(events)
            try:
                incidents.insert_many(events)
                mongo_write_success += len(events)
                latency_write_batch_ms = latency_detect_batch_ms
            except Exception:
                mongo_write_errors += len(events)
                latency_write_batch_ms = []

        # Accumulate latency samples only for threats where ingest_ts existed.
        latency_ingest_to_detect_ms.extend(latency_ingest_batch_ms)
        if latency_write_batch_ms:
            latency_detect_to_write_ms.extend(latency_write_batch_ms)
            latency_ingest_to_write_ms.extend([a + b for a, b in zip(latency_ingest_batch_ms, latency_write_batch_ms[: len(latency_ingest_batch_ms)])])

        elapsed = time.time() - start
        snapshot = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "processed_total": total_consumed,
            "threats_total": threats_total,
            "processed_rate_eps": total_consumed / max(elapsed, 1e-6),
            "mongo_write_success": mongo_write_success,
            "mongo_write_errors": mongo_write_errors,
            "consumer_lag_messages": lag_messages,
            "latency_ingest_to_detect_ms_avg": float(np.mean(latency_ingest_to_detect_ms)) if latency_ingest_to_detect_ms else 0.0,
            "latency_ingest_to_detect_ms_p95": float(np.percentile(latency_ingest_to_detect_ms, 95)) if latency_ingest_to_detect_ms else 0.0,
            "latency_ingest_to_write_ms_p95": float(np.percentile(latency_ingest_to_write_ms, 95)) if latency_ingest_to_write_ms else 0.0,
            "latency_detect_to_write_ms_p95": float(np.percentile(latency_detect_to_write_ms, 95)) if latency_detect_to_write_ms else 0.0,
        }

        append_benchmark_snapshot(snapshot)
        append_jsonl(
            {
                "t": snapshot["updated_at"],
                "processed_total": total_consumed,
                "consumer_lag_messages": lag_messages,
                "processed_rate_eps": snapshot["processed_rate_eps"],
            }
        )

    checkpoint_dir = ROOT / "checkpoints" / "spark_nids_inference"
    checkpoint_location = f"file://{checkpoint_dir.as_posix()}"

    q = (
        df_parsed.writeStream.outputMode("update")
        .foreachBatch(foreach_batch)
        .option("checkpointLocation", checkpoint_location)
        .start()
    )

    print("Spark streaming inference started.")
    q.awaitTermination()


if __name__ == "__main__":
    main()

