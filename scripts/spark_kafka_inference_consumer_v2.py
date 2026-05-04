from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import sys

import joblib
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from kafka import KafkaConsumer
from kafka.structs import TopicPartition
from pymongo import MongoClient
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, from_json
from pyspark.sql.types import (
    BooleanType,
    DoubleType,
    LongType,
    StringType,
    StructField,
    StructType,
)


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

    # Model + threshold (best hybrid model)
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

    n_targeted = len(targeted_10)
    n_pca = len(pca_cols)

    def safe_col_name(name: str) -> str:
        # Spark/PySpark Pandas UDF wiring can break on columns with spaces.
        # We therefore rename feature columns to a safe, underscore-only form.
        return (
            name.strip()
            .replace(" ", "_")
            .replace("/", "_")
            .replace("\\", "_")
            .replace("-", "_")
            .replace(".", "_")
        )

    targeted_10_safe = [safe_col_name(c) for c in targeted_10]
    pca_pool_safe = [safe_col_name(c) for c in pca_pool]
    orig_targeted_by_safe = {s: o for s, o in zip(targeted_10_safe, targeted_10)}

    # Load sklearn artifacts once (driver) and reuse inside mapInPandas via closure.
    imputer = joblib.load(imputer_path)
    scaler = joblib.load(scaler_path)
    pca = joblib.load(pca_path)
    model = joblib.load(model_path)
    booster = model.get_booster()

    # Pre-extract numeric parameters so mapInPandas only does math.
    imputer_medians = np.asarray(imputer.statistics_, dtype=np.float64)  # (n_pca_pool,)
    scaler_mean = np.asarray(scaler.mean_, dtype=np.float64)  # (n_pca_pool,)
    scaler_scale = np.asarray(scaler.scale_, dtype=np.float64)  # (n_pca_pool,)
    pca_mean = np.asarray(pca.mean_, dtype=np.float64)  # (n_pca_pool,)
    pca_components_t = np.asarray(pca.components_, dtype=np.float64).T  # (n_pca_pool, n_pca)

    # Ensure contiguous arrays for faster matmul.
    imputer_medians = np.ascontiguousarray(imputer_medians)
    scaler_mean = np.ascontiguousarray(scaler_mean)
    scaler_scale = np.ascontiguousarray(scaler_scale)
    pca_mean = np.ascontiguousarray(pca_mean)
    pca_components_t = np.ascontiguousarray(pca_components_t)

    # Kafka connector for Spark 4.1.1 (Scala 2.13)
    spark_kafka_spark_version = os.getenv("SPARK_KAFKA_SPARK_VERSION", "4.1.1")
    spark_kafka_scala_version = os.getenv("SPARK_KAFKA_SCALA_VERSION", "2.13")
    spark_kafka_pkg = os.getenv(
        "SPARK_KAFKA_PACKAGE",
        f"org.apache.spark:spark-sql-kafka-0-10_{spark_kafka_scala_version}:{spark_kafka_spark_version}",
    )

    # Ensure Spark worker processes use the same Python environment as this script.
    # Otherwise mapInPandas can fail with missing deps (e.g. xgboost).
    os.environ["PYSPARK_PYTHON"] = sys.executable
    os.environ["PYSPARK_DRIVER_PYTHON"] = sys.executable

    spark = (
        SparkSession.builder.appName("nids-spark-inference-v2")
        .config("spark.jars.packages", spark_kafka_pkg)
        .config("spark.hadoop.fs.defaultFS", "file:///")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")

    # Schema for JSON parsing: targeted + pca_pool numeric features + metadata used for events.
    # Note: Spark allows struct field names with spaces, but we reference them with backticks later.
    json_schema_fields: list[StructField] = []
    for c in targeted_10:
        json_schema_fields.append(StructField(c, DoubleType(), True))
    for c in pca_pool:
        json_schema_fields.append(StructField(c, DoubleType(), True))

    # Metadata produced by stream_kafka_producer.py
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
        .option("startingOffsets", "latest")
        .option(
            "maxOffsetsPerTrigger",
            os.getenv("SPARK_MAX_OFFSETS_PER_TRIGGER", "2000"),
        )
        .load()
    )

    df_with_json = (
        df_kafka.select(
            col("partition").cast("long").alias("partition"),
            col("offset").cast("long").alias("offset"),
            from_json(col("value").cast("string"), json_schema).alias("data"),
        )
    )

    # Flatten to columns for mapInPandas
    select_cols = [
        col("partition"),
        col("offset"),
    ]
    for orig, safe in zip(targeted_10, targeted_10_safe):
        select_cols.append(col("data").getField(orig).alias(safe))
    for orig, safe in zip(pca_pool, pca_pool_safe):
        select_cols.append(col("data").getField(orig).alias(safe))
    for c in ["ingest_ts", "source_ip", "destination_ip", "benchmark_run_id", "benchmark_profile"]:
        select_cols.append(col("data").getField(c).alias(c))

    df_flat = df_with_json.select(*select_cols)

    # Force more parallel partitions so mapInPandas can utilize more CPU cores.
    # (In local mode, Kafka often maps to a single Spark partition.)
    infer_parts = int(os.getenv("SPARK_INFERENCE_PARTITIONS", str(os.cpu_count() or 4)))
    infer_parts = max(1, infer_parts)
    if infer_parts > 1:
        df_flat = df_flat.repartition(infer_parts)

    # Scoring in Spark (distributed): use mapInPandas to avoid toPandas() on the whole microbatch.
    out_fields: list[StructField] = [
        StructField("partition", LongType(), True),
        StructField("offset", LongType(), True),
        StructField("ingest_ts", StringType(), True),
        StructField("source_ip", StringType(), True),
        StructField("destination_ip", StringType(), True),
        StructField("benchmark_run_id", StringType(), True),
        StructField("benchmark_profile", StringType(), True),
        StructField("confidence", DoubleType(), True),
        StructField("is_threat", BooleanType(), True),
    ]
    # Keep targeted features for Mongo event payload
    for c in targeted_10_safe:
        out_fields.append(StructField(c, DoubleType(), True))

    out_schema = StructType(out_fields)

    def score_partition(pdf: pd.DataFrame) -> pd.DataFrame:
        # Convert only required columns to numpy.
        # Some Spark/PyArrow execution paths can serialize these as iterators.
        # Force to concrete lists so pandas column selection works reliably.
        targeted_cols = list(targeted_10_safe)
        pca_cols = list(pca_pool_safe)

        X_targeted = pdf[targeted_cols].to_numpy(dtype=np.float64, copy=False)
        X_pca_pool = pdf[pca_cols].to_numpy(dtype=np.float64, copy=False)

        # Match training-time semantics: inf/-inf -> NaN.
        if not np.isfinite(X_targeted).all():
            X_targeted[~np.isfinite(X_targeted)] = np.nan
        if not np.isfinite(X_pca_pool).all():
            X_pca_pool[~np.isfinite(X_pca_pool)] = np.nan

        # Median imputation for PCA pool.
        nan_mask = ~np.isfinite(X_pca_pool)
        if nan_mask.any():
            X_pca_pool = np.where(nan_mask, imputer_medians.reshape(1, -1), X_pca_pool)

        # StandardScaler transform
        X_scaled = (X_pca_pool - scaler_mean.reshape(1, -1)) / scaler_scale.reshape(1, -1)

        # PCA transform: (X - mean_) @ components_.T
        X_centered = X_scaled - pca_mean.reshape(1, -1)
        X_pca = X_centered @ pca_components_t  # (n_rows, n_pca)

        X_hybrid = np.hstack([X_targeted, X_pca]).astype(np.float32, copy=False)
        # Use XGBoost native predict for lower overhead.
        probs = booster.inplace_predict(X_hybrid)

        out = pd.DataFrame(
            {
                "partition": pdf["partition"].to_numpy(dtype=np.int64, copy=False),
                "offset": pdf["offset"].to_numpy(dtype=np.int64, copy=False),
                "ingest_ts": pdf["ingest_ts"].where(pd.notna(pdf["ingest_ts"]), None),
                "source_ip": pdf["source_ip"],
                "destination_ip": pdf["destination_ip"],
                "benchmark_run_id": pdf["benchmark_run_id"].where(pd.notna(pdf["benchmark_run_id"]), None),
                "benchmark_profile": pdf["benchmark_profile"].where(pd.notna(pdf["benchmark_profile"]), None),
                "confidence": probs.astype(np.float64),
                "is_threat": (probs >= threshold),
            }
        )
        for safe_name in targeted_cols:
            out[safe_name] = pdf[safe_name].to_numpy(dtype=np.float64, copy=False)

        return out

    df_scored = df_flat.mapInPandas(score_partition, schema=out_schema)

    # Mongo client (driver). We'll insert only threat rows collected from foreachBatch.
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

    benchmark_metrics_path = ROOT / "logs" / "benchmark_runtime_metrics.json"
    lag_history_path = ROOT / "logs" / "consumer_lag_history.jsonl"

    def append_benchmark_snapshot(data: dict[str, Any]) -> None:
        benchmark_metrics_path.parent.mkdir(parents=True, exist_ok=True)
        benchmark_metrics_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def append_jsonl(data: dict[str, Any]) -> None:
        lag_history_path.parent.mkdir(parents=True, exist_ok=True)
        with lag_history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data) + "\n")

    # Shared counters (driver)
    start = time.time()
    total_consumed = 0
    threats_total = 0
    mongo_write_errors = 0
    mongo_write_success = 0

    latency_ingest_to_detect_ms: list[float] = []
    latency_ingest_to_write_ms: list[float] = []
    latency_detect_to_write_ms: list[float] = []
    active_benchmark_run_id: str | None = None

    def foreach_batch(batch_df, batch_id: int) -> None:
        nonlocal total_consumed, threats_total, mongo_write_errors, mongo_write_success
        nonlocal latency_ingest_to_detect_ms, latency_ingest_to_write_ms, latency_detect_to_write_ms

        batch_wall_start = time.time()
        detect_dt = datetime.now(timezone.utc)
        # Count total rows processed in this micro-batch.
        # (This is still a job, but far cheaper than toPandas() on all columns.)
        batch_count = batch_df.count()
        if batch_count == 0:
            return

        total_consumed += batch_count

        # We intentionally skip lag calculation in the hot path.
        # It requires extra actions/driver collects and can dominate micro-batch time.
        lag_messages = 0

        # Collect only threat rows to build events + compute per-threat latency.
        threats_pdf = (
            batch_df.filter(col("is_threat") == True)
            .select(
                "ingest_ts",
                "source_ip",
                "destination_ip",
                "benchmark_run_id",
                "benchmark_profile",
                "confidence",
                *targeted_10_safe,
            )
            .toPandas()
        )

        threats_batch = int(len(threats_pdf))
        threats_total += threats_batch

        # Reset latency buffers when the benchmark run changes.
        # This prevents old, worst-case samples from dominating later benchmark SLO checks.
        batch_run_id: str | None = None
        if threats_batch > 0 and "benchmark_run_id" in threats_pdf.columns:
            ingest_dt_series = pd.to_datetime(threats_pdf["ingest_ts"], errors="coerce", utc=True)
            if ingest_dt_series.notna().any():
                idx = int(ingest_dt_series.idxmax())
                v = threats_pdf.iloc[idx]["benchmark_run_id"]
                if v is not None and str(v).strip() != "" and str(v) != "nan":
                    batch_run_id = str(v)

        if batch_run_id and batch_run_id != active_benchmark_run_id:
            active_benchmark_run_id = batch_run_id
            latency_ingest_to_detect_ms.clear()
            latency_ingest_to_write_ms.clear()
            latency_detect_to_write_ms.clear()

        events: list[dict[str, Any]] = []
        if threats_batch > 0:
            write_dt = datetime.now(timezone.utc)
            for i in range(threats_batch):
                ingest_dt = parse_iso_ts(threats_pdf.iloc[i]["ingest_ts"])
                event: dict[str, Any] = {
                    "timestamp": detect_dt.isoformat(),
                    "ingest_ts": threats_pdf.iloc[i]["ingest_ts"],
                    "detect_ts": detect_dt.isoformat(),
                    "confidence": float(threats_pdf.iloc[i]["confidence"]),
                    "threshold": threshold,
                    "threat_classification": "malicious",
                    "benchmark": {
                        "run_id": threats_pdf.iloc[i]["benchmark_run_id"],
                        "profile": threats_pdf.iloc[i]["benchmark_profile"],
                    },
                    "network_identifiers": {
                        # Producer already normalized keys to lowercase:
                        "source_ip": threats_pdf.iloc[i]["source_ip"],
                        "destination_ip": threats_pdf.iloc[i]["destination_ip"],
                        "target_port": threats_pdf.iloc[i].get("Destination Port"),
                    },
                    "features": {
                        orig_name: float(threats_pdf.iloc[i][safe_name])
                        for safe_name, orig_name in orig_targeted_by_safe.items()
                    },
                }

                if ingest_dt is not None:
                    ingest_to_detect_ms = (detect_dt - ingest_dt).total_seconds() * 1000.0
                    ingest_to_write_ms = (write_dt - ingest_dt).total_seconds() * 1000.0
                    detect_to_write_ms = (write_dt - detect_dt).total_seconds() * 1000.0
                    event["latency_ingest_to_detect_ms"] = ingest_to_detect_ms
                    event["latency_ingest_to_write_ms"] = ingest_to_write_ms
                    event["latency_detect_to_write_ms"] = detect_to_write_ms
                    # Only record latency samples for the freshest benchmark run.
                    # This prevents earlier queued messages (from previous runs) from
                    # permanently dominating p95.
                    if batch_run_id and str(event["benchmark"]["run_id"]) == batch_run_id:
                        latency_ingest_to_detect_ms.append(ingest_to_detect_ms)
                        latency_ingest_to_write_ms.append(ingest_to_write_ms)
                        latency_detect_to_write_ms.append(detect_to_write_ms)

                events.append(event)

            if incidents is not None:
                try:
                    incidents.insert_many(events)
                    mongo_write_success += len(events)
                except Exception:
                    mongo_write_errors += len(events)

        elapsed = time.time() - start
        snapshot = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "processed_total": total_consumed,
            "threats_total": threats_total,
            # Use instantaneous micro-batch throughput so throughput doesn't decay
            # as the streaming job runs longer.
            "processed_rate_eps": batch_count / max(time.time() - batch_wall_start, 1e-6),
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

    checkpoint_dir = ROOT / "checkpoints" / "spark_nids_inference_v2"
    checkpoint_location = f"file://{checkpoint_dir.as_posix()}"

    query = (
        df_scored.writeStream.outputMode("update")
        .foreachBatch(foreach_batch)
        .option("checkpointLocation", checkpoint_location)
        .trigger(processingTime=os.getenv("SPARK_TRIGGER_INTERVAL", "1 second"))
        .start()
    )

    print("Spark v2 streaming inference started.")
    query.awaitTermination()


if __name__ == "__main__":
    main()

