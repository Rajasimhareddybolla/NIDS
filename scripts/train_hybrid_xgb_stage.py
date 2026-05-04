from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import classification_report, precision_recall_fscore_support
from xgboost import XGBClassifier


ROOT = Path(__file__).resolve().parent.parent
TRAIN_PATH = ROOT / "data" / "processed" / "train_hybrid_10plus25.pkl"
VAL_PATH = ROOT / "data" / "processed" / "validation_hybrid_10plus25.pkl"
MODEL_PATH = ROOT / "models" / "hybrid_xgb_10plus25.joblib"
LOG_DIR = ROOT / "logs"
DYNAMIC_PATH = ROOT / "config" / "dynamic_params.json"


def main() -> None:
    if not TRAIN_PATH.exists() or not VAL_PATH.exists():
        raise SystemExit("Hybrid artifacts missing. Run `make build-hybrid` first.")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)

    train = pd.read_pickle(TRAIN_PATH)
    val = pd.read_pickle(VAL_PATH)
    label_col = "target_binary"
    excluded = {"Label", "target_binary"}
    feature_cols = [c for c in train.columns if c not in excluded]

    x_train = train[feature_cols]
    y_train = train[label_col].astype(int).to_numpy()
    x_val = val[feature_cols]
    y_val = val[label_col].astype(int).to_numpy()

    benign = int((y_train == 0).sum())
    attack = int((y_train == 1).sum())
    scale_pos_weight = benign / max(attack, 1)

    model = XGBClassifier(
        n_estimators=400,
        max_depth=8,
        learning_rate=0.06,
        subsample=0.9,
        colsample_bytree=0.9,
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=42,
        n_jobs=6,
        scale_pos_weight=scale_pos_weight,
        tree_method="hist",
    )
    model.fit(x_train, y_train)

    proba = model.predict_proba(x_val)[:, 1]
    thresholds = np.arange(0.30, 0.96, 0.01)
    best = {"threshold": 0.5, "precision": 0.0, "recall": 0.0, "f1": -1.0}
    fallback = {"threshold": 0.5, "precision": -1.0, "recall": 0.0, "f1": -1.0}

    for t in thresholds:
        pred = (proba >= t).astype(int)
        p, r, f1, _ = precision_recall_fscore_support(y_val, pred, average="binary", zero_division=0)
        if (p > fallback["precision"]) or (p == fallback["precision"] and f1 > fallback["f1"]):
            fallback = {"threshold": float(t), "precision": float(p), "recall": float(r), "f1": float(f1)}
        if p >= 0.95 and f1 > best["f1"]:
            best = {"threshold": float(t), "precision": float(p), "recall": float(r), "f1": float(f1)}

    chosen = fallback if best["f1"] < 0 else best
    y_pred = (proba >= chosen["threshold"]).astype(int)
    report = classification_report(y_val, y_pred, output_dict=True, zero_division=0)

    joblib.dump(model, MODEL_PATH)
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    eval_path = LOG_DIR / f"hybrid_xgb_eval_{run_ts}.json"
    payload = {
        "run_timestamp": run_ts,
        "model_path": str(MODEL_PATH),
        "feature_count": len(feature_cols),
        "scale_pos_weight": float(scale_pos_weight),
        "selected_threshold": chosen["threshold"],
        "precision_attack": chosen["precision"],
        "recall_attack": chosen["recall"],
        "f1_attack": chosen["f1"],
        "classification_report": report,
    }
    eval_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    dynamic = json.loads(DYNAMIC_PATH.read_text(encoding="utf-8")) if DYNAMIC_PATH.exists() else {}
    dynamic["scale_pos_weight"] = float(scale_pos_weight)
    dynamic["classification_threshold"] = float(chosen["threshold"])
    dynamic["validation_f1_attack_binary"] = float(chosen["f1"])
    DYNAMIC_PATH.write_text(json.dumps(dynamic, indent=2), encoding="utf-8")

    print(json.dumps(payload, indent=2))
    print(f"\nSaved eval report: {eval_path}")


if __name__ == "__main__":
    main()
