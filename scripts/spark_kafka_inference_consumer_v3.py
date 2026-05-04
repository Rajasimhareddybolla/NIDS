from __future__ import annotations

import json
import os
import time
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, from_json
from pyspark.sql.types import (
    DoubleType,
    LongType,
    StringType,
    StructField,
    StructType,
)

from pymongo import MongoClient


ROOT = Path(__file__).resolve().parent.parent


def parse_iso_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def safe_col_name(name: str) -> str:
    # Spark/PySpark/Pandas UDF plumbing can be fragile with spaces in column names.
    return (
        name.strip()
        .replace(" ", "_")
        .replace("/", "_")
        .replace("\\", "_")
        .replace("-", "_")
        .replace(".", "_")
    )


def load_dynamic_params() -> dict[str, Any]:
    p = ROOT / "config" / "dynamic_params.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> None:
    load_dotenv(ROOT / ".env")

    bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    topic = os.getenv("KAFKA_TOPIC", "network-telemetry")

    mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    mongo_db = os.getenv("MONGODB_DB", "nids")
    incidents_col = os.getenv("MONGODB_INCIDENTS_COLLECTION", "security_incidents")

    dynamic = load_dynamic_params()
    model_path = Path(
        os.getenv(
            "STREAM_MODEL_PATH",
            str(dynamic.get("high_recall_model_path", ROOT / "models" / "xgb_combo_targeted10_pca25.joblib")),
        )
    )
    threshold_default = float(dynamic.get("classification_threshold", 0.3))
    threshold = float(os.getenv("STREAM_THRESHOLD", str(threshold_default)))
    selected_feature_space = str(dynamic.get("high_recall_feature_space", "hybrid_10plus25"))

    preprocess_bundle_path = ROOT / "models" / "hybrid_preprocess_bundle.json"
    preprocess_dir = ROOT / "models"
    imputer_path = preprocess_dir / "hybrid_preprocess_imputer.joblib"
    scaler_path = preprocess_dir / "hybrid_preprocess_scaler.joblib"
    pca_path = preprocess_dir / "hybrid_preprocess_pca.joblib"

    if not model_path.exists():
        raise SystemExit(f"Model not found: {model_path}")
    model = joblib.load(model_path)
    booster = model.get_booster()
    feature_cols = list(getattr(model, "feature_names_in_", []))

    targeted_10: list[str] = []
    pca_pool: list[str] = []
    pca_cols: list[str] = []
    targeted_10_safe: list[str] = []
    pca_pool_safe: list[str] = []
    targeted_orig_by_safe: dict[str, str] = {}
    imputer = None
    scaler = None
    pca = None
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
        targeted_10_safe = [safe_col_name(c) for c in targeted_10]
        pca_pool_safe = [safe_col_name(c) for c in pca_pool]
        targeted_orig_by_safe = {safe: orig for safe, orig in zip(targeted_10_safe, targeted_10)}
        imputer = joblib.load(imputer_path)
        scaler = joblib.load(scaler_path)
        pca = joblib.load(pca_path)
    elif not feature_cols:
        raise SystemExit("Selected non-hybrid model has no feature_names_in_.")

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

    # Kafka connector package (Spark 4.1.1 + Scala 2.13)
    spark_kafka_spark_version = os.getenv("SPARK_KAFKA_SPARK_VERSION", "4.1.1")
    spark_kafka_scala_version = os.getenv("SPARK_KAFKA_SCALA_VERSION", "2.13")
    spark_kafka_pkg = os.getenv(
        "SPARK_KAFKA_PACKAGE",
        f"org.apache.spark:spark-sql-kafka-0-10_{spark_kafka_scala_version}:{spark_kafka_spark_version}",
    )

    # Ensure Spark worker processes use the same Python environment.
    os.environ["PYSPARK_PYTHON"] = sys.executable
    os.environ["PYSPARK_DRIVER_PYTHON"] = sys.executable

    spark = (
        SparkSession.builder.appName("nids-spark-inference-v3")
        .config("spark.jars.packages", spark_kafka_pkg)
        .config("spark.hadoop.fs.defaultFS", "file:///")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")

    # Parse JSON into a struct using Spark (so we avoid Python json.loads per row).
    json_schema_fields: list[StructField] = []
    source_features = targeted_10 + pca_pool if selected_feature_space == "hybrid_10plus25" else feature_cols
    for c in source_features:
        json_schema_fields.append(StructField(c, DoubleType(), True))

    json_schema_fields.extend(
        [
            StructField("ingest_ts", StringType(), True),
            StructField("source_ip", StringType(), True),
            StructField("destination_ip", StringType(), True),
            StructField("benchmark_run_id", StringType(), True),
            StructField("benchmark_profile", StringType(), True),
        ]
    )
    json_schema = StructType(json_schema_fields)

    df_kafka = (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", bootstrap)
        .option("subscribe", topic)
        .option("startingOffsets", os.getenv("SPARK_STARTING_OFFSETS", "latest"))
        .option("maxOffsetsPerTrigger", os.getenv("SPARK_MAX_OFFSETS_PER_TRIGGER", "2000"))
        .load()
    )

    df_parsed = df_kafka.select(
        col("partition").cast("long").alias("partition"),
        col("offset").cast("long").alias("offset"),
        from_json(col("value").cast("string"), json_schema).alias("data"),
    )

    select_cols = [col("partition"), col("offset")]
    # Features: parse struct field -> safe alias
    if selected_feature_space == "hybrid_10plus25":
        for orig, safe in zip(targeted_10, targeted_10_safe):
            select_cols.append(col("data").getField(orig).alias(safe))
        for orig, safe in zip(pca_pool, pca_pool_safe):
            select_cols.append(col("data").getField(orig).alias(safe))
    else:
        for orig in feature_cols:
            select_cols.append(col("data").getField(orig).alias(safe_col_name(orig)))
    # Metadata
    for meta in ["ingest_ts", "source_ip", "destination_ip", "benchmark_run_id", "benchmark_profile"]:
        select_cols.append(col("data").getField(meta).alias(meta))

    df_flat = df_parsed.select(*select_cols)

    benchmark_metrics_path = ROOT / "logs" / "benchmark_runtime_metrics.json"

    total_consumed = 0
    threats_total = 0
    mongo_write_errors = 0
    mongo_write_success = 0

    latency_ingest_to_detect_ms: list[float] = []
    latency_ingest_to_write_ms: list[float] = []
    latency_detect_to_write_ms: list[float] = []
    prof_count_to_pandas_ms: list[float] = []
    prof_preprocess_ms: list[float] = []
    prof_inference_ms: list[float] = []
    prof_build_events_ms: list[float] = []
    prof_mongo_write_ms: list[float] = []
    prof_total_batch_ms: list[float] = []
    start_ts = time.time()

    def append_benchmark_snapshot(data: dict[str, Any]) -> None:
        benchmark_metrics_path.parent.mkdir(parents=True, exist_ok=True)
        benchmark_metrics_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def foreach_batch(batch_df, batch_id: int) -> None:
        nonlocal total_consumed, threats_total, mongo_write_errors, mongo_write_success
        nonlocal latency_ingest_to_detect_ms, latency_ingest_to_write_ms, latency_detect_to_write_ms

        batch_t0 = time.perf_counter()
        t0 = time.perf_counter()
        batch_count = batch_df.count()
        if batch_count == 0:
            return

        total_consumed += batch_count
        detect_dt = datetime.now(timezone.utc)
        pdf = batch_df.toPandas()
        prof_count_to_pandas_ms.append((time.perf_counter() - t0) * 1000.0)

        # Vectorized feature prep
        t1 = time.perf_counter()
        if selected_feature_space == "hybrid_10plus25":
            x_targeted = pdf[targeted_10_safe].to_numpy(dtype=np.float64, copy=False)
            x_pca_pool = pdf[pca_pool_safe].to_numpy(dtype=np.float64, copy=False)

            if not np.isfinite(x_targeted).all():
                x_targeted[~np.isfinite(x_targeted)] = np.nan
            if not np.isfinite(x_pca_pool).all():
                x_pca_pool[~np.isfinite(x_pca_pool)] = np.nan

            x_pca_imp = imputer.transform(x_pca_pool)
            x_pca_scaled = scaler.transform(x_pca_imp)
            x_pca_vec = pca.transform(x_pca_scaled)
            x_infer = np.hstack([x_targeted, x_pca_vec]).astype(np.float32, copy=False)
        else:
            raw_safe = [safe_col_name(c) for c in feature_cols]
            x_infer = pdf[raw_safe].to_numpy(dtype=np.float32, copy=False)
            if not np.isfinite(x_infer).all():
                x_infer[~np.isfinite(x_infer)] = np.nan
        prof_preprocess_ms.append((time.perf_counter() - t1) * 1000.0)

        t2 = time.perf_counter()
        probs = booster.inplace_predict(x_infer)
        is_threat = probs >= threshold
        prof_inference_ms.append((time.perf_counter() - t2) * 1000.0)

        threat_indices = np.where(is_threat)[0]
        threats_batch = int(threat_indices.size)
        threats_total += threats_batch

        events: list[dict[str, Any]] = []
        latency_detect_batch_ms: list[float] = []
        latency_ingest_batch_ms: list[float] = []
        latency_write_batch_ms: list[float] = []
        mongo_inserted = 0

        t3 = time.perf_counter()
        if threats_batch > 0:
            write_dt = datetime.now(timezone.utc)
            for i in threat_indices:
                ingest_dt = parse_iso_ts(pdf.iloc[i]["ingest_ts"])
                if selected_feature_space == "hybrid_10plus25":
                    event_features = {orig: float(pdf.iloc[i][safe]) for safe, orig in targeted_orig_by_safe.items()}
                    target_port = event_features.get("Destination Port")
                else:
                    event_features = {}
                    target_port = pdf.iloc[i].get(safe_col_name("Destination Port"))

                event: dict[str, Any] = {
                    "timestamp": write_dt.isoformat(),
                    "ingest_ts": pdf.iloc[i]["ingest_ts"],
                    "detect_ts": detect_dt.isoformat(),
                    "confidence": float(probs[i]),
                    "threshold": threshold,
                    "threat_classification": "malicious",
                    "benchmark": {
                        "run_id": pdf.iloc[i]["benchmark_run_id"],
                        "profile": pdf.iloc[i]["benchmark_profile"],
                    },
                    "network_identifiers": {
                        "source_ip": pdf.iloc[i]["source_ip"],
                        "destination_ip": pdf.iloc[i]["destination_ip"],
                        "target_port": target_port,
                    },
                    "features": event_features,
                }

                if ingest_dt is not None:
                    ingest_to_detect_ms = (detect_dt - ingest_dt).total_seconds() * 1000.0
                    ingest_to_write_ms = (write_dt - ingest_dt).total_seconds() * 1000.0
                    detect_to_write_ms = (write_dt - detect_dt).total_seconds() * 1000.0
                    event["latency_ingest_to_detect_ms"] = ingest_to_detect_ms
                    event["latency_ingest_to_write_ms"] = ingest_to_write_ms
                    event["latency_detect_to_write_ms"] = detect_to_write_ms
                    latency_ingest_batch_ms.append(ingest_to_detect_ms)
                    latency_write_batch_ms.append(ingest_to_write_ms)
                    latency_detect_batch_ms.append(detect_to_write_ms)

                events.append(event)
        prof_build_events_ms.append((time.perf_counter() - t3) * 1000.0)

        t4 = time.perf_counter()
        if threats_batch > 0:
            if incidents is not None and events:
                try:
                    incidents.insert_many(events)
                    mongo_inserted = len(events)
                    mongo_write_success += len(events)
                except Exception:
                    mongo_write_errors += len(events)
        prof_mongo_write_ms.append((time.perf_counter() - t4) * 1000.0)

        if latency_ingest_batch_ms:
            latency_ingest_to_detect_ms.extend(latency_ingest_batch_ms)
        if latency_write_batch_ms:
            latency_ingest_to_write_ms.extend(latency_write_batch_ms)
        if latency_detect_batch_ms:
            latency_detect_to_write_ms.extend(latency_detect_batch_ms)

        elapsed = time.time() - start_ts
        processed_rate_eps = total_consumed / max(elapsed, 1e-6)
        batch_total_ms = (time.perf_counter() - batch_t0) * 1000.0
        prof_total_batch_ms.append(batch_total_ms)
        active_eps = batch_count / max(batch_total_ms / 1000.0, 1e-6)
        snapshot = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "processed_total": total_consumed,
            "threats_total": threats_total,
            "processed_rate_eps": processed_rate_eps,
            "mongo_write_success": mongo_write_success,
            "mongo_write_errors": mongo_write_errors,
            "consumer_lag_messages": 0,
            "latency_ingest_to_detect_ms_avg": float(np.mean(latency_ingest_to_detect_ms)) if latency_ingest_to_detect_ms else 0.0,
            "latency_ingest_to_detect_ms_p95": float(np.percentile(latency_ingest_to_detect_ms, 95)) if latency_ingest_to_detect_ms else 0.0,
            "latency_ingest_to_write_ms_p95": float(np.percentile(latency_ingest_to_write_ms, 95)) if latency_ingest_to_write_ms else 0.0,
            "latency_detect_to_write_ms_p95": float(np.percentile(latency_detect_to_write_ms, 95)) if latency_detect_to_write_ms else 0.0,
            "profile_last_batch": {
                "batch_id": int(batch_id),
                "batch_rows": int(batch_count),
                "threat_rows": int(threats_batch),
                "mongo_inserted": int(mongo_inserted),
                "count_to_pandas_ms": round(prof_count_to_pandas_ms[-1], 3),
                "preprocess_ms": round(prof_preprocess_ms[-1], 3),
                "inference_ms": round(prof_inference_ms[-1], 3),
                "build_events_ms": round(prof_build_events_ms[-1], 3),
                "mongo_write_ms": round(prof_mongo_write_ms[-1], 3),
                "total_batch_ms": round(batch_total_ms, 3),
                "active_eps": round(active_eps, 3),
            },
            "profile_avg_ms": {
                "count_to_pandas_ms": round(float(np.mean(prof_count_to_pandas_ms)), 3) if prof_count_to_pandas_ms else 0.0,
                "preprocess_ms": round(float(np.mean(prof_preprocess_ms)), 3) if prof_preprocess_ms else 0.0,
                "inference_ms": round(float(np.mean(prof_inference_ms)), 3) if prof_inference_ms else 0.0,
                "build_events_ms": round(float(np.mean(prof_build_events_ms)), 3) if prof_build_events_ms else 0.0,
                "mongo_write_ms": round(float(np.mean(prof_mongo_write_ms)), 3) if prof_mongo_write_ms else 0.0,
                "total_batch_ms": round(float(np.mean(prof_total_batch_ms)), 3) if prof_total_batch_ms else 0.0,
            },
        }
        append_benchmark_snapshot(snapshot)

    checkpoint_dir = ROOT / "checkpoints" / "spark_nids_inference_v3"
    checkpoint_location = f"file://{checkpoint_dir.as_posix()}"

    query = (
        df_flat.writeStream.outputMode("update")
        .foreachBatch(foreach_batch)
        .option("checkpointLocation", checkpoint_location)
        .trigger(processingTime=os.getenv("SPARK_TRIGGER_INTERVAL", "1 second"))
        .start()
    )

    print("Spark v3 streaming inference started.")
    query.awaitTermination()


if __name__ == "__main__":
    main()

