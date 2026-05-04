from __future__ import annotations

import json
import os
import time
import hashlib
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv
from kafka import KafkaProducer


ROOT = Path(__file__).resolve().parent.parent


def _pick(rec: dict, keys: list[str]) -> object:
    for k in keys:
        v = rec.get(k)
        if v is not None and str(v).strip() != "":
            return v
    return None


def _derive_ips_from_flow_id(flow_id: object) -> tuple[object, object]:
    if flow_id is None:
        return None, None
    s = str(flow_id).strip()
    if "-" not in s:
        return None, None
    parts = s.split("-")
    if len(parts) < 2:
        return None, None
    return parts[0], parts[1]


def _synthetic_ip(seed: str, base: str) -> str:
    h = hashlib.sha1(seed.encode("utf-8")).digest()
    a = int(h[0]) % 254 + 1
    b = int(h[1]) % 254 + 1
    return f"{base}.{a}.{b}"


def main() -> None:
    load_dotenv(ROOT / ".env")
    bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    topic = os.getenv("KAFKA_TOPIC", "network-telemetry")
    source_csv = os.getenv(
        "STREAM_SOURCE_CSV",
        str(ROOT / "data" / "raw" / "Friday-WorkingHours-Afternoon-DDos.pcap_ISCX.csv"),
    )
    max_rows = int(os.getenv("STREAM_MAX_ROWS", "50000"))
    sleep_ms = int(os.getenv("STREAM_SLEEP_MS", "1"))
    benchmark_run_id = os.getenv("BENCHMARK_RUN_ID", "").strip()
    benchmark_profile = os.getenv("BENCHMARK_PROFILE", "").strip()

    csv_path = Path(source_csv)
    if not csv_path.exists():
        raise SystemExit(f"Source CSV not found: {csv_path}")

    producer = KafkaProducer(
        bootstrap_servers=[bootstrap],
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )

    df = pd.read_csv(csv_path, low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    if max_rows > 0:
        df = df.head(max_rows)

    print(f"Producing {len(df)} rows -> topic={topic} bootstrap={bootstrap}")
    sent = 0
    for idx, row in df.iterrows():
        rec = {str(k).strip(): (None if pd.isna(v) else v) for k, v in row.to_dict().items()}
        src_ip = _pick(rec, ["Source IP", "Src IP", "src_ip"])
        dst_ip = _pick(rec, ["Destination IP", "Dst IP", "dst_ip"])
        if src_ip is None or dst_ip is None:
            d_src, d_dst = _derive_ips_from_flow_id(rec.get("Flow ID"))
            src_ip = src_ip or d_src
            dst_ip = dst_ip or d_dst
        # Dataset variant may not include IP fields; synthesize deterministic IPs for simulation.
        if src_ip is None:
            src_ip = _synthetic_ip(f"src|{idx}|{rec.get('Destination Port')}", "10.10")
        if dst_ip is None:
            dst_ip = _synthetic_ip(f"dst|{idx}|{rec.get('Destination Port')}", "172.16")
        rec["source_ip"] = src_ip
        rec["destination_ip"] = dst_ip
        rec["ingest_ts"] = datetime.now(timezone.utc).isoformat()
        if benchmark_run_id:
            rec["benchmark_run_id"] = benchmark_run_id
        if benchmark_profile:
            rec["benchmark_profile"] = benchmark_profile
        producer.send(topic, value=rec)
        sent += 1
        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000.0)

    producer.flush()
    print(f"Done. Sent rows: {sent}")


if __name__ == "__main__":
    main()
