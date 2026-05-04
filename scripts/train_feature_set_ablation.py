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
MODELS_DIR = ROOT / "models"
LOG_DIR = ROOT / "logs"
DYNAMIC_PATH = ROOT / "config" / "dynamic_params.json"


def choose_threshold(y_true: np.ndarray, prob: np.ndarray) -> dict[str, float]:
    thresholds = np.arange(0.05, 0.96, 0.01)
    constrained_best = {"threshold": 0.5, "precision": 0.0, "recall": 0.0, "f1": -1.0}
    fallback_best = {"threshold": 0.5, "precision": -1.0, "recall": 0.0, "f1": -1.0}

    for t in thresholds:
        pred = (prob >= t).astype(int)
        p, r, f1, _ = precision_recall_fscore_support(y_true, pred, average="binary", zero_division=0)
        current = {"threshold": float(t), "precision": float(p), "recall": float(r), "f1": float(f1)}

        if (p > fallback_best["precision"]) or (p == fallback_best["precision"] and r > fallback_best["recall"]):
            fallback_best = current
        if p >= 0.95 and r > constrained_best["recall"]:
            constrained_best = current

    return fallback_best if constrained_best["recall"] <= 0 else constrained_best


def train_one(name: str, x_train: pd.DataFrame, y_train: np.ndarray, x_val: pd.DataFrame, y_val: np.ndarray, scale_pos_weight: float) -> dict:
    model = XGBClassifier(
        n_estimators=650,
        max_depth=10,
        learning_rate=0.04,
        subsample=0.95,
        colsample_bytree=0.9,
        min_child_weight=1,
        reg_lambda=3.0,
        gamma=0.0,
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=42,
        n_jobs=6,
        tree_method="hist",
        scale_pos_weight=scale_pos_weight,
    )
    model.fit(x_train, y_train)
    prob = model.predict_proba(x_val)[:, 1]
    selected = choose_threshold(y_val, prob)
    pred = (prob >= selected["threshold"]).astype(int)
    report = classification_report(y_val, pred, output_dict=True, zero_division=0)

    model_path = MODELS_DIR / f"xgb_{name}.joblib"
    joblib.dump(model, model_path)

    return {
        "name": name,
        "feature_count": int(x_train.shape[1]),
        "selected_threshold": selected["threshold"],
        "precision_attack": selected["precision"],
        "recall_attack": selected["recall"],
        "f1_attack": selected["f1"],
        "classification_report": report,
        "model_path": str(model_path),
    }


def main() -> None:
    if not TRAIN_PATH.exists() or not VAL_PATH.exists():
        raise SystemExit("Hybrid feature artifacts not found. Run `make build-hybrid` first.")

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    train = pd.read_pickle(TRAIN_PATH)
    val = pd.read_pickle(VAL_PATH)

    y_train = train["target_binary"].astype(int).to_numpy()
    y_val = val["target_binary"].astype(int).to_numpy()

    all_feature_cols = [c for c in train.columns if c not in {"Label", "target_binary"}]
    pca_cols = [c for c in all_feature_cols if c.startswith("PCA_")]
    targeted_cols = [c for c in all_feature_cols if c not in set(pca_cols)]
    combo_cols = targeted_cols + pca_cols

    benign = int((y_train == 0).sum())
    attack = int((y_train == 1).sum())
    scale_pos_weight = benign / max(attack, 1)

    results = []
    results.append(
        train_one(
            "only_targeted10",
            train[targeted_cols],
            y_train,
            val[targeted_cols],
            y_val,
            scale_pos_weight,
        )
    )
    results.append(
        train_one(
            "only_pca25",
            train[pca_cols],
            y_train,
            val[pca_cols],
            y_val,
            scale_pos_weight,
        )
    )
    results.append(
        train_one(
            "combo_targeted10_pca25",
            train[combo_cols],
            y_train,
            val[combo_cols],
            y_val,
            scale_pos_weight,
        )
    )

    best = max(results, key=lambda r: (r["f1_attack"], r["recall_attack"], r["precision_attack"]))
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = LOG_DIR / f"xgb_feature_set_ablation_{run_ts}.json"
    payload = {
        "run_timestamp": run_ts,
        "train_rows": int(len(train)),
        "validation_rows": int(len(val)),
        "scale_pos_weight": float(scale_pos_weight),
        "feature_sets": {
            "targeted_cols": targeted_cols,
            "pca_cols": pca_cols,
            "combo_cols_count": len(combo_cols),
        },
        "results": results,
        "best_by_f1_attack": best["name"],
    }
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Update dynamic params from best setup.
    dynamic = json.loads(DYNAMIC_PATH.read_text(encoding="utf-8")) if DYNAMIC_PATH.exists() else {}
    dynamic["scale_pos_weight"] = float(scale_pos_weight)
    dynamic["classification_threshold"] = float(best["selected_threshold"])
    dynamic["validation_f1_attack_binary"] = float(best["f1_attack"])
    DYNAMIC_PATH.write_text(json.dumps(dynamic, indent=2), encoding="utf-8")

    print(json.dumps(payload, indent=2))
    print(f"\nSaved ablation report: {report_path}")


if __name__ == "__main__":
    main()
