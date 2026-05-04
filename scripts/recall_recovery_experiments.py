from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import precision_recall_fscore_support
from xgboost import XGBClassifier


ROOT = Path(__file__).resolve().parent.parent
TRAIN_HYB = ROOT / "data" / "processed" / "train_hybrid_10plus25.pkl"
VAL_HYB = ROOT / "data" / "processed" / "validation_hybrid_10plus25.pkl"
TRAIN_RAW = ROOT / "data" / "processed" / "train_xgb_ready.pkl"
VAL_RAW = ROOT / "data" / "processed" / "validation_xgb_ready.pkl"
LOG_DIR = ROOT / "logs"


def prf(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    p, r, f1, _ = precision_recall_fscore_support(y_true, y_pred, average="binary", zero_division=0)
    return {"precision": float(p), "recall": float(r), "f1": float(f1)}


def threshold_sweep(y_true: np.ndarray, prob: np.ndarray, thresholds: list[float]) -> list[dict[str, float]]:
    out = []
    for t in thresholds:
        pred = (prob >= t).astype(int)
        row = {"threshold": float(t)}
        row.update(prf(y_true, pred))
        out.append(row)
    return out


def train_xgb(
    x_train: pd.DataFrame,
    y_train: np.ndarray,
    x_val: pd.DataFrame,
    y_val: np.ndarray,
    scale_pos_weight: float,
) -> tuple[XGBClassifier, np.ndarray]:
    model = XGBClassifier(
        n_estimators=650,
        max_depth=10,
        learning_rate=0.04,
        subsample=0.95,
        colsample_bytree=0.9,
        min_child_weight=1,
        reg_lambda=3.0,
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=42,
        n_jobs=6,
        tree_method="hist",
        scale_pos_weight=scale_pos_weight,
    )
    model.fit(x_train, y_train)
    prob = model.predict_proba(x_val)[:, 1]
    return model, prob


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not (TRAIN_HYB.exists() and VAL_HYB.exists() and TRAIN_RAW.exists() and VAL_RAW.exists()):
        raise SystemExit("Required train/validation artifacts missing.")

    train_h = pd.read_pickle(TRAIN_HYB)
    val_h = pd.read_pickle(VAL_HYB)
    y_train = train_h["target_binary"].astype(int).to_numpy()
    y_val = val_h["target_binary"].astype(int).to_numpy()
    x_train_h = train_h[[c for c in train_h.columns if c not in {"Label", "target_binary"}]]
    x_val_h = val_h[[c for c in val_h.columns if c not in {"Label", "target_binary"}]]

    benign = int((y_train == 0).sum())
    attack = int((y_train == 1).sum())
    base_spw = benign / max(attack, 1)

    # 1) threshold sweep on baseline hybrid model
    _, prob_h = train_xgb(x_train_h, y_train, x_val_h, y_val, scale_pos_weight=base_spw)
    thresh_rows = threshold_sweep(y_val, prob_h, [0.30, 0.15, 0.10, 0.05])

    # 2) scale_pos_weight override sweep on hybrid features
    spw_tests = [base_spw, base_spw * 2, 50.0, 100.0, 200.0]
    spw_rows = []
    for spw in spw_tests:
        _, prob = train_xgb(x_train_h, y_train, x_val_h, y_val, scale_pos_weight=spw)
        metrics = threshold_sweep(y_val, prob, [0.05])[0]
        metrics["scale_pos_weight"] = float(spw)
        spw_rows.append(metrics)

    # 3) no-PCA bypass (raw 78-ish numeric features)
    train_r = pd.read_pickle(TRAIN_RAW)
    val_r = pd.read_pickle(VAL_RAW)
    y_train_r = train_r["target_binary"].astype(int).to_numpy()
    y_val_r = val_r["target_binary"].astype(int).to_numpy()
    x_train_r = train_r[[c for c in train_r.columns if c not in {"Label", "__source_file", "target_binary", "target_multiclass"}]].copy()
    x_val_r = val_r[[c for c in val_r.columns if c not in {"Label", "__source_file", "target_binary", "target_multiclass"}]].copy()
    # ensure numeric matrix with NaNs preserved
    for c in x_train_r.columns:
        x_train_r[c] = pd.to_numeric(x_train_r[c], errors="coerce")
    for c in x_val_r.columns:
        x_val_r[c] = pd.to_numeric(x_val_r[c], errors="coerce")
    x_train_r.replace([np.inf, -np.inf], np.nan, inplace=True)
    x_val_r.replace([np.inf, -np.inf], np.nan, inplace=True)

    _, prob_raw = train_xgb(x_train_r, y_train_r, x_val_r, y_val_r, scale_pos_weight=base_spw)
    nopca_rows = threshold_sweep(y_val_r, prob_raw, [0.30, 0.15, 0.10, 0.05])

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = {
        "run_timestamp": run_ts,
        "train_rows": int(len(train_h)),
        "validation_rows": int(len(val_h)),
        "base_scale_pos_weight": float(base_spw),
        "threshold_sweep_hybrid": thresh_rows,
        "scale_pos_weight_sweep_hybrid_at_t005": spw_rows,
        "no_pca_threshold_sweep": nopca_rows,
        "no_pca_feature_count": int(x_train_r.shape[1]),
    }
    path = LOG_DIR / f"recall_recovery_experiments_{run_ts}.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"\nSaved report: {path}")


if __name__ == "__main__":
    main()
