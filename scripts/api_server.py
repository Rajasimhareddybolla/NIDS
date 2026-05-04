from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import json

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pymongo import MongoClient


load_dotenv()
app = FastAPI(title="NIDS Query API", version="1.0.0")

mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
db_name = os.getenv("MONGODB_DB", "nids")
incidents_name = os.getenv("MONGODB_INCIDENTS_COLLECTION", "security_incidents")
campaigns_name = os.getenv("MONGODB_CAMPAIGNS_COLLECTION", "attack_campaigns")

client = MongoClient(mongo_uri)
db = client[db_name]
incidents = db[incidents_name]
campaigns = db[campaigns_name]
ROOT = Path(__file__).resolve().parent.parent


def clean_docs(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for d in docs:
        x = dict(d)
        x.pop("_id", None)
        out.append(x)
    return out


def latest_json(prefix: str) -> dict[str, Any]:
    try:
        files = sorted((ROOT / "logs").glob(f"{prefix}_*.json"))
        if not files:
            return {}
        return json_load(files[-1])
    except Exception:
        return {}


def json_load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def summarize_test_file(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    stem = path.stem
    test_name = stem.split("_20")[0] if "_20" in stem else stem
    config: dict[str, Any] = {}
    result: dict[str, Any] = {}
    inference = ""

    if test_name == "pca_full_comparison":
        config = {
            "feature_count": payload.get("feature_count"),
            "total_rows_available": payload.get("total_rows_available"),
            "presets": [x.get("preset") for x in payload.get("results", [])],
        }
        result = {
            "k95_values": [x.get("k95") for x in payload.get("results", [])],
            "k99_values": [x.get("k99") for x in payload.get("results", [])],
            "total_time_s": [x.get("total_s") for x in payload.get("results", [])],
        }
        inference = "PCA component stability across fast/balanced/full scales."
    elif test_name == "xgb_feature_set_ablation":
        config = {
            "train_rows": payload.get("train_rows"),
            "validation_rows": payload.get("validation_rows"),
            "scale_pos_weight": payload.get("scale_pos_weight"),
        }
        result = {
            "best_by_f1_attack": payload.get("best_by_f1_attack"),
            "scores": [
                {
                    "name": r.get("name"),
                    "precision_attack": r.get("precision_attack"),
                    "recall_attack": r.get("recall_attack"),
                    "f1_attack": r.get("f1_attack"),
                }
                for r in payload.get("results", [])
            ],
        }
        inference = "Hybrid combo (10+25) selected as strongest tested feature set."
    elif test_name == "recall_recovery_experiments":
        config = {
            "base_scale_pos_weight": payload.get("base_scale_pos_weight"),
            "train_rows": payload.get("train_rows"),
        }
        result = {
            "threshold_sweep": payload.get("threshold_sweep_hybrid", []),
            "scale_pos_weight_sweep": payload.get("scale_pos_weight_sweep_hybrid_at_t005", []),
        }
        inference = "Threshold tuning helped recall; aggressive class-weight sweeps degraded."
    elif test_name == "multiclass_vs_binary":
        config = {"run_timestamp": payload.get("run_timestamp")}
        result = payload.get("comparison", {})
        inference = payload.get("recommendation", "")
    elif test_name == "benchmark_baseline":
        config = {"host": payload.get("host"), "generated_at": payload.get("generated_at")}
        result = {
            "max_profile_eps": payload.get("max_profile_eps"),
            "profiles": [
                {
                    "profile": p.get("profile"),
                    "rows": p.get("rows"),
                    "producer_eps": p.get("producer_eps"),
                    "consumer_eps": p.get("consumer_processed_rate_eps"),
                }
                for p in payload.get("profiles", [])
            ],
        }
        inference = payload.get("bottleneck_hint", "")
    elif test_name == "benchmark_full_plan":
        config = payload.get("inputs", {})
        result = payload.get("phases", {})
        inference = "Full industry-style benchmark matrix for ramp/burst/soak/recovery."
    else:
        config = {k: payload.get(k) for k in ("timestamp", "run_timestamp", "generated_at") if k in payload}
        result = {"keys": list(payload.keys())[:12]}
        inference = "Captured run artifact."

    return {
        "test_name": test_name,
        "file": path.name,
        "config": config,
        "result": result,
        "inference": inference,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        client.admin.command("ping")
        return {"ok": True, "mongo": "connected"}
    except Exception as e:
        return {"ok": False, "mongo": "disconnected", "error": str(e)}


@app.get("/incidents/recent")
def incidents_recent(limit: int = 100, minutes: int = 60) -> list[dict[str, Any]]:
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        docs = list(
            incidents.find({"timestamp": {"$gte": cutoff.isoformat()}})
            .sort("timestamp", -1)
            .limit(limit)
        )
        return clean_docs(docs)
    except Exception:
        return []


@app.get("/campaigns/recent")
def campaigns_recent(limit: int = 100, hours: int = 24) -> list[dict[str, Any]]:
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        docs = list(
            campaigns.find({"timestamp_start": {"$gte": cutoff.isoformat()}})
            .sort("timestamp_start", -1)
            .limit(limit)
        )
        return clean_docs(docs)
    except Exception:
        return []


@app.get("/campaigns/{attacker_ip}")
def campaigns_by_ip(attacker_ip: str, limit: int = 100) -> list[dict[str, Any]]:
    try:
        docs = list(
            campaigns.find({"attacker_ip": attacker_ip})
            .sort("timestamp_start", -1)
            .limit(limit)
        )
        return clean_docs(docs)
    except Exception:
        return []


@app.get("/metrics/summary")
def metrics_summary(hours: int = 24) -> dict[str, Any]:
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        cutoff_iso = cutoff.isoformat()
        inc_count = incidents.count_documents({"timestamp": {"$gte": cutoff_iso}})
        camp_count = campaigns.count_documents({"timestamp_start": {"$gte": cutoff_iso}})

        conf_docs = list(
            incidents.aggregate(
                [
                    {"$match": {"timestamp": {"$gte": cutoff_iso}}},
                    {"$group": {"_id": None, "avg_conf": {"$avg": "$confidence"}}},
                ]
            )
        )
        avg_conf = float(conf_docs[0]["avg_conf"]) if conf_docs else 0.0

        top_ips_docs = list(
            incidents.aggregate(
                [
                    {"$match": {"timestamp": {"$gte": cutoff_iso}}},
                    {
                        "$group": {
                            "_id": "$network_identifiers.source_ip",
                            "count": {"$sum": 1},
                        }
                    },
                    {"$sort": {"count": -1}},
                    {"$limit": 5},
                ]
            )
        )
        top_ips = [{"source_ip": d.get("_id") or "unknown", "count": int(d["count"])} for d in top_ips_docs]

        return {
            "hours": hours,
            "incidents": int(inc_count),
            "campaigns": int(camp_count),
            "avg_confidence": avg_conf,
            "top_source_ips": top_ips,
        }
    except Exception:
        return {"hours": hours, "incidents": 0, "campaigns": 0, "avg_confidence": 0.0, "top_source_ips": []}


@app.get("/metrics/timeseries")
def metrics_timeseries(minutes: int = 120, bucket_minutes: int = 5) -> dict[str, Any]:
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        cutoff_iso = cutoff.isoformat()
        bucket_ms = bucket_minutes * 60 * 1000
        pipeline = [
            {"$match": {"timestamp": {"$gte": cutoff_iso}}},
            {
                "$project": {
                    "ts": {"$toDate": "$timestamp"},
                    "confidence": 1,
                }
            },
            {
                "$group": {
                    "_id": {
                        "$toDate": {
                            "$subtract": [
                                {"$toLong": "$ts"},
                                {"$mod": [{"$toLong": "$ts"}, bucket_ms]},
                            ]
                        }
                    },
                    "count": {"$sum": 1},
                    "avg_conf": {"$avg": "$confidence"},
                }
            },
            {"$sort": {"_id": 1}},
        ]
        docs = list(incidents.aggregate(pipeline))
        points = [
            {
                "t": d["_id"].isoformat() if hasattr(d["_id"], "isoformat") else str(d["_id"]),
                "count": int(d["count"]),
                "avg_conf": float(d["avg_conf"]) if d.get("avg_conf") is not None else 0.0,
            }
            for d in docs
        ]
        return {"minutes": minutes, "bucket_minutes": bucket_minutes, "points": points}
    except Exception:
        return {"minutes": minutes, "bucket_minutes": bucket_minutes, "points": []}


@app.get("/metrics/throughput")
def metrics_throughput(minutes: int = 5) -> dict[str, Any]:
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        cutoff_iso = cutoff.isoformat()
        count = incidents.count_documents({"timestamp": {"$gte": cutoff_iso}})
        per_sec = float(count) / float(minutes * 60) if minutes > 0 else 0.0
        return {"minutes": minutes, "incidents": int(count), "events_per_sec": per_sec}
    except Exception:
        return {"minutes": minutes, "incidents": 0, "events_per_sec": 0.0}


@app.get("/metrics/model_eval")
def metrics_model_eval() -> dict[str, Any]:
    # Try multiclass eval first, fallback to binary ablation output.
    multi = latest_json("multiclass_eval")
    if multi:
        return {
            "type": "multiclass",
            "labels": multi.get("labels_evaluated", []),
            "confusion_matrix": multi.get("confusion_matrix", []),
            "gate_result": multi.get("gate_result", {}),
        }
    abl = latest_json("xgb_feature_set_ablation")
    if abl:
        best_name = abl.get("best_by_f1_attack")
        results = abl.get("results", [])
        best = next((r for r in results if r.get("name") == best_name), results[-1] if results else {})
        return {
            "type": "binary",
            "labels": ["benign", "attack"],
            "confusion_matrix": [],
            "gate_result": {},
            "classification_report": best.get("classification_report", {}),
        }
    return {"type": "none", "labels": [], "confusion_matrix": [], "gate_result": {}}


@app.get("/metrics/hotspots")
def metrics_hotspots(hours: int = 24, limit: int = 8) -> dict[str, Any]:
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        cutoff_iso = cutoff.isoformat()

        top_ports = list(
            incidents.aggregate(
                [
                    {"$match": {"timestamp": {"$gte": cutoff_iso}}},
                    {"$group": {"_id": "$network_identifiers.target_port", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                    {"$limit": limit},
                ]
            )
        )
        top_dest_ips = list(
            incidents.aggregate(
                [
                    {"$match": {"timestamp": {"$gte": cutoff_iso}}},
                    {"$group": {"_id": "$network_identifiers.destination_ip", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                    {"$limit": limit},
                ]
            )
        )
        top_types = list(
            incidents.aggregate(
                [
                    {"$match": {"timestamp": {"$gte": cutoff_iso}}},
                    {"$group": {"_id": "$threat_classification", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                    {"$limit": limit},
                ]
            )
        )
        return {
            "hours": hours,
            "top_ports": [{"port": x.get("_id"), "count": int(x["count"])} for x in top_ports],
            "top_destination_ips": [
                {"destination_ip": x.get("_id") or "unknown", "count": int(x["count"])}
                for x in top_dest_ips
            ],
            "top_threat_types": [{"type": x.get("_id") or "unknown", "count": int(x["count"])} for x in top_types],
        }
    except Exception:
        return {"hours": hours, "top_ports": [], "top_destination_ips": [], "top_threat_types": []}


@app.get("/metrics/final")
def metrics_final() -> dict[str, Any]:
    # Targets from the original project intent and discussion.
    targets = {
        "precision_attack_min": 0.85,
        "recall_attack_target": 0.80,
        "f1_attack_target": 0.60,
    }
    dynamic = {}
    dyn_path = ROOT / "config" / "dynamic_params.json"
    if dyn_path.exists():
        dynamic = json_load(dyn_path)

    ablation = latest_json("xgb_feature_set_ablation")
    selected = {}
    if ablation:
        res = ablation.get("results", [])
        combo = [r for r in res if r.get("name") == "combo_targeted10_pca25"]
        selected = combo[0] if combo else (res[-1] if res else {})

    precision = float(selected.get("precision_attack", dynamic.get("multiclass_precision_attack", 0.0) or 0.0))
    recall = float(selected.get("recall_attack", dynamic.get("multiclass_recall_attack", 0.0) or 0.0))
    f1 = float(selected.get("f1_attack", dynamic.get("validation_f1_attack_binary", 0.0) or 0.0))
    threshold = float(dynamic.get("classification_threshold", selected.get("selected_threshold", 0.0) or 0.0))

    return {
        "model": {
            "feature_set": "combo_targeted10_pca25",
            "precision_attack": precision,
            "recall_attack": recall,
            "f1_attack": f1,
            "threshold": threshold,
            "pca_k": dynamic.get("pca_k"),
            "pca_variance_retained": dynamic.get("pca_variance_retained"),
        },
        "targets": targets,
        "status": {
            "precision_pass": precision >= targets["precision_attack_min"],
            "recall_pass": recall >= targets["recall_attack_target"],
            "f1_pass": f1 >= targets["f1_attack_target"],
        },
    }


@app.get("/metrics/benchmark")
def metrics_benchmark() -> dict[str, Any]:
    bench = latest_json("benchmark_baseline")
    runtime_path = ROOT / "logs" / "benchmark_runtime_metrics.json"
    runtime = {}
    if runtime_path.exists():
        runtime = json_load(runtime_path)
    return {
        "latest_baseline": bench,
        "runtime_snapshot": runtime,
    }


@app.get("/metrics/consumer_lag")
def metrics_consumer_lag(limit: int = 300) -> dict[str, Any]:
    entries = read_jsonl(ROOT / "logs" / "consumer_lag_history.jsonl")
    if limit > 0:
        entries = entries[-limit:]
    return {"points": entries}


@app.get("/metrics/test_catalog")
def metrics_test_catalog() -> dict[str, Any]:
    files = sorted((ROOT / "logs").glob("*.json"))
    tests = []
    for f in files:
        if f.name == "benchmark_runtime_metrics.json":
            continue
        try:
            payload = json_load(f)
            tests.append(summarize_test_file(f, payload))
        except Exception:
            continue
    return {"total_tests": len(tests), "tests": tests}


@app.get("/metrics/slo")
def metrics_slo() -> dict[str, Any]:
    final = metrics_final()
    throughput = metrics_throughput(minutes=5)
    bench_data = metrics_benchmark()
    runtime = bench_data.get("runtime_snapshot", {})

    targets = {
        "end_to_end_latency_sec_max": 5.0,
        "throughput_sustained_eps_target": 10000.0,
        "peak_spike_eps_target": 100000.0,
        "mongo_write_rate_eps_max": 500.0,
    }

    ingest_to_write_ms = float(runtime.get("latency_ingest_to_write_ms_p95", 0.0) or 0.0)
    ingest_to_detect_ms = float(runtime.get("latency_ingest_to_detect_ms_p95", 0.0) or 0.0)
    detect_to_write_ms = float(runtime.get("latency_detect_to_write_ms_p95", 0.0) or 0.0)
    processed_rate = float(runtime.get("processed_rate_eps", 0.0) or 0.0)
    mongo_rate = float(throughput.get("events_per_sec", 0.0) or 0.0)
    baseline = bench_data.get("latest_baseline", {})
    burst_peak = float(baseline.get("max_profile_eps", 0.0) or 0.0)

    def status_if_available(measured: float, has_data: bool, predicate: bool) -> str:
        if not has_data:
            return "UNVERIFIED"
        return "PASS" if predicate else "FAIL"

    status = {
        "end_to_end_latency": status_if_available(
            ingest_to_write_ms,
            ingest_to_write_ms > 0.0,
            (ingest_to_write_ms / 1000.0) <= targets["end_to_end_latency_sec_max"],
        ),
        "throughput_sustained": status_if_available(
            processed_rate,
            processed_rate > 0.0,
            processed_rate >= targets["throughput_sustained_eps_target"],
        ),
        "peak_spike_handling": status_if_available(
            burst_peak,
            burst_peak > 0.0,
            burst_peak >= targets["peak_spike_eps_target"],
        ),
        "mongo_write_rate": status_if_available(
            mongo_rate,
            True,
            mongo_rate <= targets["mongo_write_rate_eps_max"],
        ),
    }

    return {
        "targets": targets,
        "measured": {
            "end_to_end_latency_sec_p95": (ingest_to_write_ms / 1000.0) if ingest_to_write_ms > 0 else None,
            "ingest_to_detect_latency_sec_p95": (ingest_to_detect_ms / 1000.0)
            if ingest_to_detect_ms > 0
            else None,
            "detect_to_write_latency_sec_p95": (detect_to_write_ms / 1000.0)
            if detect_to_write_ms > 0
            else None,
            "throughput_sustained_eps": processed_rate if processed_rate > 0 else None,
            "peak_spike_eps_observed": burst_peak if burst_peak > 0 else None,
            "mongo_write_rate_eps": mongo_rate,
        },
        "status": status,
        "model_status": final.get("status", {}),
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>NIDS Live Dashboard</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; background: #0b1020; color: #e8eefc; }
    .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
    .card { background: #141b2d; border-radius: 8px; padding: 12px; border: 1px solid #28324d; }
    .title { color: #8fa4d6; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }
    .value { font-size: 26px; font-weight: 700; margin-top: 8px; }
    .row { display: grid; grid-template-columns: 2fr 1fr; gap: 12px; }
    .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; margin-left:8px; }
    .ok { background:#204d2a; color:#b7ffca; }
    .bad { background:#5b2020; color:#ffd0d0; }
    #tsChart, #pieChart { background: #141b2d; border: 1px solid #28324d; border-radius: 8px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #28324d; padding: 8px; text-align: left; font-size: 13px; }
  </style>
</head>
<body>
  <h2>NIDS Real-Time Dashboard</h2>
  <div class="grid">
    <div class="card"><div class="title">Incidents (24h)</div><div class="value" id="incidents">0</div></div>
    <div class="card"><div class="title">Campaigns (24h)</div><div class="value" id="campaigns">0</div></div>
    <div class="card"><div class="title">Avg Confidence</div><div class="value" id="avgConf">0.00</div></div>
    <div class="card"><div class="title">API Health</div><div class="value" id="health">-</div></div>
  </div>
  <div class="grid">
    <div class="card"><div class="title">Model Precision (Attack)</div><div class="value" id="mPrecision">0.000</div></div>
    <div class="card"><div class="title">Model Recall (Attack)</div><div class="value" id="mRecall">0.000</div></div>
    <div class="card"><div class="title">Model F1 (Attack)</div><div class="value" id="mF1">0.000</div></div>
    <div class="card"><div class="title">Decision Threshold</div><div class="value" id="mThr">0.00</div></div>
  </div>
  <div class="card" style="margin-bottom:12px;">
    <div class="title">Target vs Actual Status</div>
    <div id="targetStatus"></div>
  </div>
  <div class="card" style="margin-bottom:12px;">
    <div class="title">Operational SLO Status</div>
    <div id="sloStatus"></div>
  </div>
  <div class="row">
    <div id="tsChart" style="height:380px;"></div>
    <div id="pieChart" style="height:380px;"></div>
  </div>
  <div class="row" style="margin-top:12px;">
    <div id="portChart" style="height:340px;"></div>
    <div id="typeChart" style="height:340px;"></div>
  </div>
  <div class="row" style="margin-top:12px;">
    <div id="cmChart" style="height:340px;"></div>
    <div class="card">
      <div class="title">Throughput (Last 5m)</div>
      <div class="value" id="eps">0.0/s</div>
      <div id="epsCount" style="margin-top:10px;color:#8fa4d6;"></div>
    </div>
  </div>
  <div class="card" style="margin-top:12px;">
    <div class="title">Top Source IPs</div>
    <table>
      <thead><tr><th>Source IP</th><th>Incidents</th></tr></thead>
      <tbody id="ipRows"></tbody>
    </table>
  </div>
  <div class="card" style="margin-top:12px;">
    <div class="title">Recent Incidents (Where It Happens)</div>
    <table>
      <thead><tr><th>Time</th><th>Source IP</th><th>Destination IP</th><th>Port</th><th>Confidence</th><th>Type</th></tr></thead>
      <tbody id="incidentRows"></tbody>
    </table>
  </div>
  <div class="card" style="margin-top:12px;">
    <div class="title">Recent Campaigns</div>
    <table>
      <thead><tr><th>Window Start</th><th>Attacker IP</th><th>Events</th><th>Confidence Score</th><th>Primary Type</th></tr></thead>
      <tbody id="campaignRows"></tbody>
    </table>
  </div>

<script>
async function fetchJson(url) {
  const r = await fetch(url);
  return await r.json();
}

async function render() {
  const [health, summary, ts, hotspot, finalMetrics, recentIncidents, recentCampaigns, throughput, modelEval, slo] = await Promise.all([
    fetchJson('/health'),
    fetchJson('/metrics/summary?hours=24'),
    fetchJson('/metrics/timeseries?minutes=180&bucket_minutes=5'),
    fetchJson('/metrics/hotspots?hours=24&limit=8'),
    fetchJson('/metrics/final'),
    fetchJson('/incidents/recent?limit=10&minutes=180'),
    fetchJson('/campaigns/recent?limit=10&hours=24'),
    fetchJson('/metrics/throughput?minutes=5'),
    fetchJson('/metrics/model_eval'),
    fetchJson('/metrics/slo')
  ]);

  document.getElementById('incidents').textContent = summary.incidents ?? 0;
  document.getElementById('campaigns').textContent = summary.campaigns ?? 0;
  document.getElementById('avgConf').textContent = (summary.avg_confidence ?? 0).toFixed(3);
  document.getElementById('health').textContent = health.ok ? 'OK' : 'DOWN';
  document.getElementById('mPrecision').textContent = (finalMetrics.model?.precision_attack ?? 0).toFixed(3);
  document.getElementById('mRecall').textContent = (finalMetrics.model?.recall_attack ?? 0).toFixed(3);
  document.getElementById('mF1').textContent = (finalMetrics.model?.f1_attack ?? 0).toFixed(3);
  document.getElementById('mThr').textContent = (finalMetrics.model?.threshold ?? 0).toFixed(2);
  document.getElementById('eps').textContent = `${(throughput.events_per_sec ?? 0).toFixed(2)}/s`;
  document.getElementById('epsCount').textContent = `${throughput.incidents ?? 0} incidents in last ${throughput.minutes ?? 5} minutes`;

  const st = finalMetrics.status || {};
  document.getElementById('targetStatus').innerHTML = `
    <div>Precision >= target <span class="badge ${st.precision_pass ? 'ok':'bad'}">${st.precision_pass ? 'PASS':'FAIL'}</span></div>
    <div>Recall >= target <span class="badge ${st.recall_pass ? 'ok':'bad'}">${st.recall_pass ? 'PASS':'FAIL'}</span></div>
    <div>F1 >= target <span class="badge ${st.f1_pass ? 'ok':'bad'}">${st.f1_pass ? 'PASS':'FAIL'}</span></div>
  `;
  const ss = slo.status || {};
  document.getElementById('sloStatus').innerHTML = `
    <div>Latency p95 <= 5s <span class="badge ${ss.end_to_end_latency === 'PASS' ? 'ok':'bad'}">${ss.end_to_end_latency || 'UNVERIFIED'}</span></div>
    <div>Sustained throughput >= 10k/s <span class="badge ${ss.throughput_sustained === 'PASS' ? 'ok':'bad'}">${ss.throughput_sustained || 'UNVERIFIED'}</span></div>
    <div>Peak spike >= 100k/s <span class="badge ${ss.peak_spike_handling === 'PASS' ? 'ok':'bad'}">${ss.peak_spike_handling || 'UNVERIFIED'}</span></div>
    <div>Mongo write rate <= 500/s <span class="badge ${ss.mongo_write_rate === 'PASS' ? 'ok':'bad'}">${ss.mongo_write_rate || 'UNVERIFIED'}</span></div>
  `;

  const rows = summary.top_source_ips || [];
  document.getElementById('ipRows').innerHTML =
    rows.map(x => `<tr><td>${x.source_ip}</td><td>${x.count}</td></tr>`).join('') ||
    '<tr><td colspan="2">No data</td></tr>';

  const points = ts.points || [];
  Plotly.newPlot('tsChart', [
    { x: points.map(p => p.t), y: points.map(p => p.count), type: 'scatter', mode: 'lines+markers', name: 'Incidents' },
    { x: points.map(p => p.t), y: points.map(p => p.avg_conf), type: 'scatter', mode: 'lines', name: 'Avg Confidence', yaxis: 'y2' }
  ], {
    title: 'Incident Trend',
    paper_bgcolor: '#141b2d',
    plot_bgcolor: '#141b2d',
    font: { color: '#e8eefc' },
    yaxis: { title: 'Incident Count' },
    yaxis2: { title: 'Avg Confidence', overlaying: 'y', side: 'right', rangemode: 'tozero' },
    margin: { t: 40, l: 50, r: 50, b: 40 }
  }, {displayModeBar:false});

  Plotly.newPlot('pieChart', [{
    labels: rows.map(r => r.source_ip),
    values: rows.map(r => r.count),
    type: 'pie',
    textinfo: 'label+percent'
  }], {
    title: 'Top Source IP Share',
    paper_bgcolor: '#141b2d',
    font: { color: '#e8eefc' },
    margin: { t: 40, l: 10, r: 10, b: 10 }
  }, {displayModeBar:false});

  const ports = hotspot.top_ports || [];
  Plotly.newPlot('portChart', [{
    x: ports.map(p => String(p.port)),
    y: ports.map(p => p.count),
    type: 'bar',
    name: 'Incidents by Port'
  }], {
    title: 'Top Target Ports',
    paper_bgcolor: '#141b2d',
    plot_bgcolor: '#141b2d',
    font: { color: '#e8eefc' },
    margin: { t: 40, l: 40, r: 20, b: 40 }
  }, {displayModeBar:false});

  const types = hotspot.top_threat_types || [];
  Plotly.newPlot('typeChart', [{
    x: types.map(t => t.type),
    y: types.map(t => t.count),
    type: 'bar',
    name: 'Threat Types'
  }], {
    title: 'Threat Type Distribution',
    paper_bgcolor: '#141b2d',
    plot_bgcolor: '#141b2d',
    font: { color: '#e8eefc' },
    margin: { t: 40, l: 40, r: 20, b: 40 }
  }, {displayModeBar:false});

  if ((modelEval.confusion_matrix || []).length > 0) {
    Plotly.newPlot('cmChart', [{
      z: modelEval.confusion_matrix,
      x: modelEval.labels || [],
      y: modelEval.labels || [],
      type: 'heatmap'
    }], {
      title: 'Confusion Matrix',
      paper_bgcolor: '#141b2d',
      plot_bgcolor: '#141b2d',
      font: { color: '#e8eefc' },
      margin: { t: 40, l: 60, r: 20, b: 60 }
    }, {displayModeBar:false});
  } else {
    Plotly.newPlot('cmChart', [{
      x: ['No matrix available'], y: [1], type: 'bar'
    }], {
      title: 'Confusion Matrix',
      paper_bgcolor: '#141b2d',
      plot_bgcolor: '#141b2d',
      font: { color: '#e8eefc' }
    }, {displayModeBar:false});
  }

  document.getElementById('incidentRows').innerHTML =
    (recentIncidents || []).map(x => {
      const ni = x.network_identifiers || {};
      return `<tr>
        <td>${x.timestamp || ''}</td>
        <td>${ni.source_ip || 'unknown'}</td>
        <td>${ni.destination_ip || 'unknown'}</td>
        <td>${ni.target_port ?? ''}</td>
        <td>${(x.confidence ?? 0).toFixed ? x.confidence.toFixed(3) : x.confidence}</td>
        <td>${x.threat_classification || 'malicious'}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="6">No incidents</td></tr>';

  document.getElementById('campaignRows').innerHTML =
    (recentCampaigns || []).map(x => `<tr>
      <td>${x.timestamp_start || ''}</td>
      <td>${x.attacker_ip || 'unknown'}</td>
      <td>${x.aggregated_events ?? 0}</td>
      <td>${(x.confidence_score ?? 0).toFixed ? x.confidence_score.toFixed(3) : x.confidence_score}</td>
      <td>${x.threat_classification || 'malicious'}</td>
    </tr>`).join('') || '<tr><td colspan="5">No campaigns</td></tr>';
}

render();
setInterval(render, 15000);
</script>
</body>
</html>
"""


@app.get("/dashboard/tests", response_class=HTMLResponse)
def dashboard_tests() -> str:
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>NIDS Tests Dashboard</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; background: #0b1020; color: #e8eefc; }
    .card { background: #141b2d; border: 1px solid #28324d; border-radius: 8px; padding: 12px; margin-bottom: 12px; }
    pre { white-space: pre-wrap; word-break: break-word; background:#0d1426; padding:10px; border-radius:6px; }
    .title { color: #8fa4d6; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; margin-bottom: 8px; }
    .grid { display:grid; grid-template-columns: repeat(4, 1fr); gap:12px; margin-bottom:12px; }
    .value { font-size: 24px; font-weight: 700; }
    .controls { display:grid; grid-template-columns: 1fr 1fr 2fr auto; gap:10px; margin-bottom:12px; }
    input, select { background:#0d1426; color:#e8eefc; border:1px solid #28324d; border-radius:6px; padding:8px; }
    .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; margin-left:8px; }
    .ok { background:#204d2a; color:#b7ffca; }
    .bad { background:#5b2020; color:#ffd0d0; }
    .warn { background:#5a4b1c; color:#ffe9a3; }
  </style>
</head>
<body>
  <h2>NIDS Unified Test Dashboard</h2>
  <div class="grid">
    <div class="card"><div class="title">Total Tests</div><div class="value" id="totalTests">0</div></div>
    <div class="card"><div class="title">SLO PASS</div><div class="value" id="sloPass">0</div></div>
    <div class="card"><div class="title">SLO FAIL</div><div class="value" id="sloFail">0</div></div>
    <div class="card"><div class="title">SLO UNVERIFIED</div><div class="value" id="sloUnverified">0</div></div>
  </div>
  <div class="card">
    <div class="title">Latest Bottleneck</div>
    <div id="bottleneckText">-</div>
  </div>
  <div class="controls">
    <select id="typeFilter"><option value="all">All test types</option></select>
    <input id="dateFilter" placeholder="Date contains (e.g. 20260429)"/>
    <input id="searchFilter" placeholder="Search config/results/inference"/>
    <button onclick="render()">Apply</button>
  </div>
  <div class="card">
    <div class="title">Consumer Lag Trend</div>
    <div id="lagChart" style="height:300px;"></div>
  </div>
  <div id="tests"></div>
<script>
async function fetchJson(url){ const r = await fetch(url); return r.json(); }
function includesText(obj, q){
  if (!q) return true;
  try { return JSON.stringify(obj).toLowerCase().includes(q.toLowerCase()); }
  catch { return false; }
}
async function render(){
  const [catalog, lag, slo] = await Promise.all([
    fetchJson('/metrics/test_catalog'),
    fetchJson('/metrics/consumer_lag?limit=500'),
    fetchJson('/metrics/slo')
  ]);
  const tests = catalog.tests || [];
  document.getElementById('totalTests').textContent = tests.length;
  const st = slo.status || {};
  const vals = [st.end_to_end_latency, st.throughput_sustained, st.peak_spike_handling, st.mongo_write_rate];
  document.getElementById('sloPass').textContent = vals.filter(v => v === 'PASS').length;
  document.getElementById('sloFail').textContent = vals.filter(v => v === 'FAIL').length;
  document.getElementById('sloUnverified').textContent = vals.filter(v => v === 'UNVERIFIED').length;
  document.getElementById('bottleneckText').textContent =
    tests.find(t => t.test_name === 'benchmark_baseline')?.inference || 'No bottleneck note available';

  const typeFilter = document.getElementById('typeFilter');
  const currentType = typeFilter.value || 'all';
  const typeSet = Array.from(new Set(tests.map(t => t.test_name))).sort();
  typeFilter.innerHTML = `<option value="all">All test types</option>` +
    typeSet.map(t => `<option value="${t}" ${t===currentType ? 'selected' : ''}>${t}</option>`).join('');
  const dateQ = (document.getElementById('dateFilter').value || '').trim();
  const searchQ = (document.getElementById('searchFilter').value || '').trim();

  const pts = lag.points || [];
  Plotly.newPlot('lagChart', [{
    x: pts.map(p => p.t),
    y: pts.map(p => p.consumer_lag_messages || 0),
    type: 'scatter',
    mode: 'lines+markers',
    name: 'Consumer Lag'
  }], {
    title: 'Consumer Lag (messages)',
    paper_bgcolor: '#141b2d',
    plot_bgcolor: '#141b2d',
    font: { color: '#e8eefc' },
    margin: { t: 40, l: 50, r: 20, b: 40 }
  }, {displayModeBar:false});

  const root = document.getElementById('tests');
  const filtered = tests.filter(t => {
    if (currentType !== 'all' && t.test_name !== currentType) return false;
    if (dateQ && !t.file.includes(dateQ)) return false;
    if (searchQ && !includesText(t, searchQ)) return false;
    return true;
  });
  root.innerHTML = filtered.map(t => `
    <div class="card">
      <div class="title">${t.test_name} - ${t.file}</div>
      <strong>What test is this?</strong>
      <pre>${t.test_name}</pre>
      <strong>Config</strong>
      <pre>${JSON.stringify(t.config, null, 2)}</pre>
      <strong>Results</strong>
      <pre>${JSON.stringify(t.result, null, 2)}</pre>
      <strong>Inference</strong>
      <pre>${t.inference || 'N/A'}</pre>
    </div>
  `).join('') || '<div class="card">No tests match current filters.</div>';
}
render();
setInterval(render, 15000);
</script>
</body>
</html>
"""
