from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"


def latest_baseline() -> dict:
    files = sorted(LOGS.glob("benchmark_baseline_*.json"))
    if not files:
        return {}
    return json.loads(files[-1].read_text(encoding="utf-8"))


def main() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    baseline = latest_baseline()
    local_max_eps = float(baseline.get("max_profile_eps", 0.0) or 0.0)

    plan = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {"latest_baseline_max_eps": local_max_eps},
        "phases": {
            "sustained_ramp": {
                "steps_eps": [max(100.0, local_max_eps * x) for x in (0.5, 0.8, 1.0, 1.2)],
                "duration_minutes_each": 10,
            },
            "burst_spike": {
                "burst_eps_candidates": [max(500.0, local_max_eps * x) for x in (2.0, 5.0, 10.0)],
                "burst_duration_sec": 30,
                "recovery_window_sec": 120,
            },
            "soak": {"duration_minutes": 60, "target_eps": max(100.0, local_max_eps * 0.7)},
            "recovery": {
                "actions": ["restart_consumer", "restart_mongo", "restart_api"],
                "measure": ["lag_drain_time_sec", "error_spike", "steady_state_recovery_sec"],
            },
        },
        "industry_metrics": [
            "e2e_latency_p50_p95_p99",
            "ingress_eps",
            "processed_eps",
            "persisted_eps",
            "consumer_lag",
            "mongo_write_error_rate",
            "api_p95_ms",
            "host_cpu_memory_swap",
            "jvm_heap_gc_pause",
            "spark_microbatch_duration_ms",
        ],
        "slo_policy": {
            "marking": ["PASS", "FAIL", "UNVERIFIED"],
            "targets": {
                "end_to_end_latency_sec_max": 5.0,
                "throughput_sustained_eps_target": 10000.0,
                "peak_spike_eps_target": 100000.0,
                "mongo_write_rate_eps_max": 500.0,
            },
        },
    }

    json_path = LOGS / f"benchmark_full_plan_{ts}.json"
    md_path = LOGS / f"benchmark_full_plan_{ts}.md"
    json_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    md_lines = [
        "# Full Benchmark Suite Plan",
        "",
        f"- Generated at: `{plan['generated_at']}`",
        f"- Baseline local max EPS input: `{local_max_eps:.2f}`",
        "",
        "## Test Matrix",
        "",
        "- Sustained ramp: 4 steps x 10m",
        "- Burst spike: 30s bursts + 120s recovery windows",
        "- Soak: 60m at 70% local stable ceiling",
        "- Recovery: restart tests for consumer/mongo/api",
        "",
        "## SLO Targets",
        "",
        "- End-to-end latency p95 <= 5s",
        "- Sustained throughput >= 10,000 EPS",
        "- Peak spike handling >= 100,000 EPS",
        "- Mongo write rate <= 500 docs/s",
    ]
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Wrote full benchmark plan JSON: {json_path}")
    print(f"Wrote full benchmark plan MD: {md_path}")


if __name__ == "__main__":
    main()
