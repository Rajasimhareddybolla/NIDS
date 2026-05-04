from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient


def floor_window(ts: datetime, minutes: int) -> datetime:
    ts = ts.astimezone(timezone.utc)
    discard = timedelta(
        minutes=ts.minute % minutes,
        seconds=ts.second,
        microseconds=ts.microsecond,
    )
    return ts - discard


def main() -> None:
    load_dotenv()
    mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    db_name = os.getenv("MONGODB_DB", "nids")
    incidents_name = os.getenv("MONGODB_INCIDENTS_COLLECTION", "security_incidents")
    campaigns_name = os.getenv("MONGODB_CAMPAIGNS_COLLECTION", "attack_campaigns")

    window_minutes = int(os.getenv("CAMPAIGN_WINDOW_MINUTES", "5"))
    min_events = int(os.getenv("CAMPAIGN_MIN_EVENTS", "10"))
    interval_sec = int(os.getenv("CAMPAIGN_AGGREGATE_INTERVAL_SECONDS", "30"))
    lookback_minutes = int(os.getenv("CAMPAIGN_LOOKBACK_MINUTES", "30"))
    max_cycles = int(os.getenv("CAMPAIGN_MAX_CYCLES", "0"))

    client = MongoClient(mongo_uri)
    db = client[db_name]
    incidents = db[incidents_name]
    campaigns = db[campaigns_name]

    print(
        f"Aggregator started window={window_minutes}m min_events={min_events} "
        f"interval={interval_sec}s lookback={lookback_minutes}m"
    )

    cycles = 0
    while True:
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=lookback_minutes)
        cursor = incidents.find({"timestamp": {"$gte": start.isoformat()}})

        buckets: dict[tuple[str, str], list[dict]] = {}
        for doc in cursor:
            src = (
                doc.get("network_identifiers", {}).get("source_ip")
                or doc.get("source_ip")
                or "unknown"
            )
            ts_raw = doc.get("timestamp")
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except Exception:
                continue
            w_start = floor_window(ts, window_minutes).isoformat()
            buckets.setdefault((src, w_start), []).append(doc)

        upserts = 0
        for (src, w_start), events in buckets.items():
            if len(events) < min_events:
                continue
            w_start_dt = datetime.fromisoformat(w_start)
            w_end_dt = w_start_dt + timedelta(minutes=window_minutes)
            confidences = [float(e.get("confidence", 0.0)) for e in events]
            conf_score = sum(confidences) / max(len(confidences), 1)
            types = [e.get("threat_classification", "malicious") for e in events]
            primary = max(set(types), key=types.count) if types else "malicious"
            cid_raw = f"{src}|{w_start}"
            campaign_id = hashlib.sha1(cid_raw.encode("utf-8")).hexdigest()[:16]

            campaigns.update_one(
                {"campaign_id": campaign_id},
                {
                    "$set": {
                        "campaign_id": campaign_id,
                        "attacker_ip": src,
                        "timestamp_start": w_start_dt.isoformat(),
                        "timestamp_end": w_end_dt.isoformat(),
                        "aggregated_events": len(events),
                        "confidence_score": conf_score,
                        "threat_classification": primary,
                        "all_threat_types": types[:200],
                        "window_duration_sec": window_minutes * 60,
                        "updated_at": now.isoformat(),
                    }
                },
                upsert=True,
            )
            upserts += 1

        print(f"[{now.isoformat()}] campaigns_upserted={upserts}")
        cycles += 1
        if max_cycles > 0 and cycles >= max_cycles:
            print("Aggregator reached max cycles. Exiting.")
            break
        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
