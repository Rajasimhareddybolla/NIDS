from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"


def fetch_json(url: str) -> dict:
    try:
        with urlopen(url, timeout=5) as resp:  # nosec - local endpoint
            return json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError):
        return {}


def host_snapshot() -> dict:
    vm = subprocess.run(["vm_stat"], capture_output=True, text=True, check=False)
    swap = subprocess.run(["sysctl", "vm.swapusage"], capture_output=True, text=True, check=False)
    ps = subprocess.run(
        ["ps", "-Ao", "comm,rss,%cpu", "-r"],
        capture_output=True,
        text=True,
        check=False,
    )
    top_processes = []
    for line in ps.stdout.splitlines()[1:11]:
        parts = line.split()
        if len(parts) >= 3:
            top_processes.append(
                {
                    "command": parts[0],
                    "rss_kb": int(parts[1]) if parts[1].isdigit() else 0,
                    "cpu_percent": float(parts[2]) if parts[2].replace(".", "", 1).isdigit() else 0.0,
                }
            )
    return {
        "vm_stat": vm.stdout.strip(),
        "swap_usage": swap.stdout.strip(),
        "top_processes": top_processes,
    }


def run_profile(name: str, rows: int, sleep_ms: int) -> dict:
    run_id = f"{name}-{int(time.time())}"
    env = os.environ.copy()
    env["STREAM_MAX_ROWS"] = str(rows)
    env["STREAM_SLEEP_MS"] = str(sleep_ms)
    env["BENCHMARK_RUN_ID"] = run_id
    env["BENCHMARK_PROFILE"] = name

    before_bench = fetch_json("http://localhost:8000/metrics/benchmark")
    before_thr = fetch_json("http://localhost:8000/metrics/throughput?minutes=5")
    t0 = time.time()
    proc = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python3"), str(ROOT / "scripts" / "stream_kafka_producer.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.time() - t0
    time.sleep(3)
    after_bench = fetch_json("http://localhost:8000/metrics/benchmark")
    after_thr = fetch_json("http://localhost:8000/metrics/throughput?minutes=5")
    produced_eps = float(rows) / elapsed if elapsed > 0 else 0.0
    runtime = after_bench.get("runtime_snapshot", {})
    return {
        "profile": name,
        "run_id": run_id,
        "rows": rows,
        "sleep_ms": sleep_ms,
        "producer_elapsed_sec": elapsed,
        "producer_eps": produced_eps,
        "producer_rc": proc.returncode,
        "producer_stdout_tail": proc.stdout.splitlines()[-5:],
        "producer_stderr_tail": proc.stderr.splitlines()[-5:],
        "throughput_before_eps": before_thr.get("events_per_sec", 0.0),
        "throughput_after_eps": after_thr.get("events_per_sec", 0.0),
        "consumer_processed_rate_eps": runtime.get("processed_rate_eps"),
        "latency_ingest_to_write_p95_ms": runtime.get("latency_ingest_to_write_ms_p95"),
        "mongo_write_errors": runtime.get("mongo_write_errors", 0),
        "benchmark_before": before_bench,
    }


def render_markdown(report: dict) -> str:
    lines = [
        "# Baseline Benchmark Report",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Host: `{report['host']}`",
        "",
        "## Profiles",
        "",
        "| Profile | Rows | Producer EPS | Consumer EPS | P95 Ingest->Write (ms) | Mongo Errors |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for p in report["profiles"]:
        lines.append(
            f"| {p['profile']} | {p['rows']} | {p['producer_eps']:.1f} | "
            f"{float(p.get('consumer_processed_rate_eps') or 0.0):.1f} | "
            f"{float(p.get('latency_ingest_to_write_p95_ms') or 0.0):.1f} | "
            f"{int(p.get('mongo_write_errors') or 0)} |"
        )
    lines.extend(
        [
            "",
            "## Bottleneck Hint",
            "",
            report["bottleneck_hint"],
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    profiles = [
        ("low", 2000, 2),
        ("medium", 5000, 1),
        ("high_local_safe", 10000, 0),
    ]

    results = [run_profile(name, rows, sleep_ms) for name, rows, sleep_ms in profiles]
    max_eps = max((float(x.get("consumer_processed_rate_eps") or 0.0) for x in results), default=0.0)
    hint = (
        "Consumer/model inference is likely bottleneck (producer EPS exceeds consumer EPS)."
        if any(float(x.get("producer_eps", 0.0)) > float(x.get("consumer_processed_rate_eps") or 0.0) for x in results)
        else "Producer-side pacing likely bottleneck at tested settings."
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": "macOS-local",
        "profiles": results,
        "max_profile_eps": max_eps,
        "bottleneck_hint": hint,
        "host_snapshot": host_snapshot(),
    }

    json_path = LOG_DIR / f"benchmark_baseline_{ts}.json"
    md_path = LOG_DIR / f"benchmark_baseline_{ts}.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"Wrote benchmark JSON: {json_path}")
    print(f"Wrote benchmark MD: {md_path}")


if __name__ == "__main__":
    sys.exit(main())
