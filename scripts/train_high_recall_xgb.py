from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.decomposition import PCA
from sklearn.impute import SimpleImputer
from sklearn.metrics import precision_recall_fscore_support
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier


ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
MODELS_DIR = ROOT / "models"
DYNAMIC_PATH = ROOT / "config" / "dynamic_params.json"

TRAIN_READY = ROOT / "data" / "processed" / "train_recall_xgb_ready.pkl"
VAL_READY = ROOT / "data" / "processed" / "validation_recall_xgb_ready.pkl"
FRIDAY_READY = ROOT / "data" / "processed" / "friday_stress_xgb_ready.pkl"

FORBIDDEN = {"Label", "target_binary", "target_multiclass", "__source_file"}
TARGETED_10_CANDIDATES = [
    "Destination Port",
    "Flow Duration",
    "SYN Flag Count",
    "ACK Flag Count",
    "FIN Flag Count",
    "Total Fwd Packets",
    "Total Backward Packets",
    "Fwd Packet Length Max",
    "Bwd Packet Length Max",
    "Flow IAT Mean",
]


def threshold_grid() -> list[float]:
    return [round(x, 3) for x in np.arange(0.01, 0.501, 0.01)]


def score_binary(y_true: np.ndarray, prob: np.ndarray, t: float) -> dict[str, float]:
    pred = (prob >= t).astype(int)
    p, r, f1, _ = precision_recall_fscore_support(y_true, pred, average="binary", zero_division=0)
    return {"threshold": float(t), "precision": float(p), "recall": float(r), "f1": float(f1)}


def best_threshold(y_true: np.ndarray, prob: np.ndarray, min_precision: float) -> tuple[dict[str, float], list[dict[str, float]]]:
    rows = [score_binary(y_true, prob, t) for t in threshold_grid()]
    valid = [r for r in rows if r["precision"] >= min_precision]
    if valid:
        best = max(valid, key=lambda x: (x["recall"], x["f1"], x["precision"]))
    else:
        best = max(rows, key=lambda x: (x["recall"], x["f1"], x["precision"]))
    return best, rows


def per_label_recall(y_label: pd.Series, prob: np.ndarray, t: float) -> dict[str, float]:
    pred_attack = (prob >= t).astype(int)
    out: dict[str, float] = {}
    for cls, g in y_label.groupby(y_label):
        idx = g.index.to_numpy()
        true_attack = (g != "BENIGN").astype(int).to_numpy()
        p, r, f1, _ = precision_recall_fscore_support(
            true_attack, pred_attack[idx], average="binary", zero_division=0
        )
        out[str(cls)] = float(r)
    return out


def train_model(x_train: pd.DataFrame, y_train: np.ndarray, x_val: pd.DataFrame) -> tuple[XGBClassifier, np.ndarray]:
    benign = int((y_train == 0).sum())
    attack = int((y_train == 1).sum())
    spw = benign / max(attack, 1)
    model = XGBClassifier(
        n_estimators=700,
        max_depth=8,
        learning_rate=0.05,
        subsample=0.95,
        colsample_bytree=0.9,
        min_child_weight=1,
        reg_lambda=3.0,
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=42,
        n_jobs=6,
        tree_method="hist",
        scale_pos_weight=spw,
    )
    model.fit(x_train, y_train)
    prob_val = model.predict_proba(x_val)[:, 1]
    return model, prob_val


def build_hybrid(train: pd.DataFrame, val: pd.DataFrame, friday: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    feature_cols = [c for c in train.columns if c not in FORBIDDEN]
    targeted = [c for c in TARGETED_10_CANDIDATES if c in feature_cols][:10]
    pca_pool = [c for c in feature_cols if c not in targeted]
    if not targeted or len(pca_pool) < 5:
        raise SystemExit("Not enough columns for hybrid feature build.")

    pca_k = min(25, len(pca_pool))
    imp = SimpleImputer(strategy="median")
    scl = StandardScaler(with_mean=True, with_std=True)
    pca = PCA(n_components=pca_k, random_state=42)

    tr_pool = train[pca_pool]
    va_pool = val[pca_pool]
    fr_pool = friday[pca_pool]

    tr_imp = imp.fit_transform(tr_pool)
    va_imp = imp.transform(va_pool)
    fr_imp = imp.transform(fr_pool)
    tr_s = scl.fit_transform(tr_imp)
    va_s = scl.transform(va_imp)
    fr_s = scl.transform(fr_imp)
    tr_p = pca.fit_transform(tr_s)
    va_p = pca.transform(va_s)
    fr_p = pca.transform(fr_s)

    pca_cols = [f"PCA_{i+1}" for i in range(pca_k)]
    x_train = pd.concat([train[targeted].reset_index(drop=True), pd.DataFrame(tr_p, columns=pca_cols)], axis=1)
    x_val = pd.concat([val[targeted].reset_index(drop=True), pd.DataFrame(va_p, columns=pca_cols)], axis=1)
    x_friday = pd.concat([friday[targeted].reset_index(drop=True), pd.DataFrame(fr_p, columns=pca_cols)], axis=1)

    bundle = {
        "targeted_10": targeted,
        "pca_pool": pca_pool,
        "pca_k": pca_k,
        "pca_cols": pca_cols,
        "feature_cols": targeted + pca_cols,
    }
    return x_train, x_val, x_friday, {"imputer": imp, "scaler": scl, "pca": pca, "bundle": bundle}


def main() -> None:
    if not (TRAIN_READY.exists() and VAL_READY.exists() and FRIDAY_READY.exists()):
        raise SystemExit("Missing recall split artifacts. Run `make build-recall-splits` first.")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    train = pd.read_pickle(TRAIN_READY)
    val = pd.read_pickle(VAL_READY)
    friday = pd.read_pickle(FRIDAY_READY)

    y_train = train["target_binary"].astype(int).to_numpy()
    y_val = val["target_binary"].astype(int).to_numpy()
    y_friday = friday["target_binary"].astype(int).to_numpy()
    y_val_label = val["Label"].astype(str).reset_index(drop=True)
    y_friday_label = friday["Label"].astype(str).reset_index(drop=True)

    min_precision = 0.85
    feature_cols = [c for c in train.columns if c not in FORBIDDEN]

    # Candidate 1: raw numeric
    x_train_raw = train[feature_cols].copy()
    x_val_raw = val[feature_cols].copy()
    x_friday_raw = friday[feature_cols].copy()
    model_raw, prob_val_raw = train_model(x_train_raw, y_train, x_val_raw)
    best_raw, sweep_raw = best_threshold(y_val, prob_val_raw, min_precision)
    prob_friday_raw = model_raw.predict_proba(x_friday_raw)[:, 1]
    friday_raw = score_binary(y_friday, prob_friday_raw, best_raw["threshold"])

    # Candidate 2: targeted10 only
    targeted = [c for c in TARGETED_10_CANDIDATES if c in feature_cols][:10]
    model_t10, prob_val_t10 = train_model(train[targeted], y_train, val[targeted])
    best_t10, sweep_t10 = best_threshold(y_val, prob_val_t10, min_precision)
    prob_friday_t10 = model_t10.predict_proba(friday[targeted])[:, 1]
    friday_t10 = score_binary(y_friday, prob_friday_t10, best_t10["threshold"])

    # Candidate 3: hybrid 10 + PCA25
    x_train_h, x_val_h, x_friday_h, hybrid_art = build_hybrid(train, val, friday)
    model_h, prob_val_h = train_model(x_train_h, y_train, x_val_h)
    best_h, sweep_h = best_threshold(y_val, prob_val_h, min_precision)
    prob_friday_h = model_h.predict_proba(x_friday_h)[:, 1]
    friday_h = score_binary(y_friday, prob_friday_h, best_h["threshold"])

    candidates = [
        {
            "name": "raw_numeric",
            "feature_count": int(x_train_raw.shape[1]),
            "grouped_validation": best_raw,
            "friday_stress": friday_raw,
            "sweep_rows": sweep_raw,
            "per_label_recall_grouped": per_label_recall(y_val_label, prob_val_raw, best_raw["threshold"]),
            "per_label_recall_friday": per_label_recall(y_friday_label, prob_friday_raw, best_raw["threshold"]),
        },
        {
            "name": "targeted10_only",
            "feature_count": int(len(targeted)),
            "grouped_validation": best_t10,
            "friday_stress": friday_t10,
            "sweep_rows": sweep_t10,
            "per_label_recall_grouped": per_label_recall(y_val_label, prob_val_t10, best_t10["threshold"]),
            "per_label_recall_friday": per_label_recall(y_friday_label, prob_friday_t10, best_t10["threshold"]),
        },
        {
            "name": "hybrid_10plus25",
            "feature_count": int(x_train_h.shape[1]),
            "grouped_validation": best_h,
            "friday_stress": friday_h,
            "sweep_rows": sweep_h,
            "per_label_recall_grouped": per_label_recall(y_val_label, prob_val_h, best_h["threshold"]),
            "per_label_recall_friday": per_label_recall(y_friday_label, prob_friday_h, best_h["threshold"]),
        },
    ]

    winner = max(
        candidates,
        key=lambda c: (
            c["grouped_validation"]["recall"],
            c["grouped_validation"]["f1"],
            c["friday_stress"]["recall"],
            c["grouped_validation"]["precision"],
        ),
    )

    best_model_path = MODELS_DIR / "xgb_high_recall_best.joblib"
    if winner["name"] == "raw_numeric":
        joblib.dump(model_raw, best_model_path)
        selected_feature_space = "raw_numeric"
    elif winner["name"] == "targeted10_only":
        joblib.dump(model_t10, best_model_path)
        selected_feature_space = "targeted10_only"
    else:
        joblib.dump(model_h, best_model_path)
        # Refresh hybrid artifacts for streaming reproducibility.
        joblib.dump(hybrid_art["imputer"], MODELS_DIR / "hybrid_preprocess_imputer.joblib")
        joblib.dump(hybrid_art["scaler"], MODELS_DIR / "hybrid_preprocess_scaler.joblib")
        joblib.dump(hybrid_art["pca"], MODELS_DIR / "hybrid_preprocess_pca.joblib")
        (MODELS_DIR / "hybrid_preprocess_bundle.json").write_text(
            json.dumps(hybrid_art["bundle"], indent=2), encoding="utf-8"
        )
        selected_feature_space = "hybrid_10plus25"

    dynamic = json.loads(DYNAMIC_PATH.read_text(encoding="utf-8")) if DYNAMIC_PATH.exists() else {}
    dynamic["classification_threshold"] = float(winner["grouped_validation"]["threshold"])
    dynamic["high_recall_precision_attack"] = float(winner["grouped_validation"]["precision"])
    dynamic["high_recall_recall_attack"] = float(winner["grouped_validation"]["recall"])
    dynamic["high_recall_f1_attack"] = float(winner["grouped_validation"]["f1"])
    dynamic["high_recall_model_path"] = str(best_model_path)
    dynamic["high_recall_feature_space"] = selected_feature_space
    DYNAMIC_PATH.write_text(json.dumps(dynamic, indent=2), encoding="utf-8")

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = {
        "run_timestamp": run_ts,
        "rows": {
            "train_grouped": int(len(train)),
            "validation_grouped": int(len(val)),
            "friday_stress": int(len(friday)),
        },
        "precision_floor": min_precision,
        "candidates": candidates,
        "winner": winner["name"],
        "winner_metrics_grouped": winner["grouped_validation"],
        "winner_metrics_friday": winner["friday_stress"],
        "winner_model_path": str(best_model_path),
        "winner_feature_space": selected_feature_space,
    }
    report = LOG_DIR / f"high_recall_xgb_{run_ts}.json"
    report.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"\nSaved report: {report}")


if __name__ == "__main__":
    main()

