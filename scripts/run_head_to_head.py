from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
RUNTIME_PATH = ROOT / "logs" / "benchmark_runtime_metrics.json"
OUT_PATH = ROOT / "logs" / "head_to_head_results.json"


@dataclass
class CaseConfig:
    name: str
    consumer_cmd: str
    topic: str
    startup_wait_sec: int
    cleanup_cmd: str | None = None
    extra_env: dict[str, str] | None = None


def run_cmd(cmd: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        shell=True,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def stop_all_consumers() -> None:
    run_cmd(
        "pkill -f 'stream_kafka_inference_consumer.py|spark_kafka_inference_consumer_v3.py|spark_kafka_inference_consumer.py' || true"
    )


def create_topic(topic: str) -> None:
    cmd = (
        "kafka-topics --bootstrap-server localhost:9092 "
        f"--create --if-not-exists --topic '{topic}' --partitions 1 --replication-factor 1"
    )
    result = run_cmd(cmd)
    print(f"[topic] create {topic} exit_code={result.returncode}")
    for line in result.stdout.splitlines()[-3:]:
        print(f"[topic] {line}")


def read_runtime_metrics() -> dict[str, Any]:
    try:
        return json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def wait_for_progress(
    target_rows: int,
    timeout_sec: int,
    heartbeat_sec: int,
    label: str,
    consumer_proc: subprocess.Popen[str],
    stall_sec: int = 30,
) -> tuple[dict[str, Any], bool]:
    start = time.time()
    next_beat = start
    last: dict[str, Any] = {}
    last_processed = -1
    last_progress_ts = start
    last_updated_at = None

    while time.time() - start < timeout_sec:
        if consumer_proc.poll() is not None:
            print(f"[{label}] consumer exited early with code={consumer_proc.returncode}")
            return last, False

        last = read_runtime_metrics()
        processed = int(last.get("processed_total", 0) or 0)
        updated_at = last.get("updated_at")
        if processed != last_processed or updated_at != last_updated_at:
            last_progress_ts = time.time()
            last_processed = processed
            last_updated_at = updated_at

        if processed >= target_rows:
            print(f"[{label}] target reached: processed_total={processed}")
            return last, True

        if time.time() - last_progress_ts > stall_sec:
            print(
                f"[{label}] stalled for >{stall_sec}s at processed_total={processed} updated_at={updated_at}"
            )
            return last, False

        now = time.time()
        if now >= next_beat:
            lag = last.get("consumer_lag_messages", None)
            eps = last.get("processed_rate_eps", None)
            updated = last.get("updated_at", "n/a")
            print(
                f"[{label}] heartbeat: processed_total={processed} lag={lag} eps={eps} updated_at={updated}"
            )
            next_beat = now + heartbeat_sec
        time.sleep(1)

    processed = int(last.get("processed_total", 0) or 0)
    print(f"[{label}] timeout after {timeout_sec}s: processed_total={processed}")
    return last, False


def run_case(cfg: CaseConfig, rows: int, wait_timeout_sec: int, heartbeat_sec: int) -> dict[str, Any]:
    print(f"\n=== Case: {cfg.name} ===")
    print(f"[{cfg.name}] topic={cfg.topic}")

    if cfg.cleanup_cmd:
        run_cmd(cfg.cleanup_cmd)
    create_topic(cfg.topic)

    RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_PATH.write_text("{}", encoding="utf-8")

    env = os.environ.copy()
    env["KAFKA_TOPIC"] = cfg.topic
    env["KAFKA_GROUP_ID"] = f"nids-h2h-{cfg.name}-{int(time.time())}"
    if cfg.extra_env:
        env.update(cfg.extra_env)

    log_path = ROOT / "logs" / f"head_to_head_{cfg.name}_consumer.log"
    log_fh = log_path.open("w", encoding="utf-8")

    consumer = subprocess.Popen(
        cfg.consumer_cmd,
        shell=True,
        cwd=ROOT,
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    print(f"[{cfg.name}] consumer started pid={consumer.pid}, waiting {cfg.startup_wait_sec}s")
    time.sleep(cfg.startup_wait_sec)

    producer_cmd = (
        f"STREAM_MAX_ROWS={rows} STREAM_SLEEP_MS=0 "
        f"BENCHMARK_RUN_ID=h2h-{cfg.name} BENCHMARK_PROFILE=h2h "
        f"KAFKA_TOPIC='{cfg.topic}' make stream-producer"
    )
    producer_start = time.time()
    prod = run_cmd(producer_cmd)
    producer_elapsed_sec = time.time() - producer_start
    producer_eps = rows / max(producer_elapsed_sec, 1e-6)
    print(f"[{cfg.name}] producer exit_code={prod.returncode}")
    print(f"[{cfg.name}] producer_elapsed_sec={producer_elapsed_sec:.3f} producer_eps={producer_eps:.2f}")
    for line in prod.stdout.splitlines()[-3:]:
        print(f"[{cfg.name}] {line}")

    metrics, reached = wait_for_progress(
        target_rows=rows,
        timeout_sec=wait_timeout_sec,
        heartbeat_sec=heartbeat_sec,
        label=cfg.name,
        consumer_proc=consumer,
    )

    try:
        consumer.terminate()
        consumer.wait(timeout=12)
    except Exception:
        try:
            os.kill(consumer.pid, signal.SIGKILL)
        except Exception:
            pass
    finally:
        log_fh.close()

    try:
        consumer_log_tail = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-20:]
    except Exception:
        consumer_log_tail = []

    return {
        "topic": cfg.topic,
        "consumer_exit_code": consumer.returncode,
        "consumer_log_path": str(log_path),
        "consumer_log_tail": consumer_log_tail,
        "producer_exit_code": prod.returncode,
        "producer_elapsed_sec": producer_elapsed_sec,
        "producer_eps": producer_eps,
        "processed_target_reached": reached,
        "processed_total": metrics.get("processed_total"),
        "processed_rate_eps": metrics.get("processed_rate_eps"),
        "latency_ingest_to_write_ms_p95": metrics.get("latency_ingest_to_write_ms_p95"),
        "latency_detect_to_write_ms_p95": metrics.get("latency_detect_to_write_ms_p95"),
        "consumer_lag_messages": metrics.get("consumer_lag_messages"),
        "active_eps": (metrics.get("profile_last_batch") or {}).get("active_eps"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Reliable Python vs Spark head-to-head runner with heartbeat logs.")
    parser.add_argument("--rows", type=int, default=2000, help="Rows to produce per case.")
    parser.add_argument("--timeout-sec", type=int, default=180, help="Max wait per case for consumer to reach target rows.")
    parser.add_argument("--heartbeat-sec", type=int, default=5, help="Heartbeat interval while waiting.")
    args = parser.parse_args()

    stop_all_consumers()

    stamp = int(time.time())
    python_cfg = CaseConfig(
        name="python",
        consumer_cmd="make stream-consumer",
        topic=f"network-telemetry-h2h-python-{stamp}",
        startup_wait_sec=4,
        extra_env={
            "STREAM_CONSUME_MAX_MESSAGES": str(args.rows),
            "KAFKA_AUTO_OFFSET_RESET": "earliest",
        },
    )
    spark_cfg = CaseConfig(
        name="spark_v3",
        consumer_cmd="make spark-stream-consumer",
        topic=f"network-telemetry-h2h-spark-{stamp}",
        startup_wait_sec=10,
        cleanup_cmd="rm -rf checkpoints/spark_nids_inference_v3",
        extra_env={"SPARK_STARTING_OFFSETS": "earliest"},
    )

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rows": args.rows,
        "results": {
            "python": run_case(python_cfg, args.rows, args.timeout_sec, args.heartbeat_sec),
            "spark_v3": run_case(spark_cfg, args.rows, args.timeout_sec, args.heartbeat_sec),
        },
    }

    stop_all_consumers()
    OUT_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("\n=== Final Head-to-Head Result ===")
    print(json.dumps(out, indent=2))
    print(f"Saved report: {OUT_PATH}")


if __name__ == "__main__":
    main()

