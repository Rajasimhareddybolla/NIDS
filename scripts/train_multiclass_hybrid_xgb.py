from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import classification_report, confusion_matrix, precision_recall_fscore_support
from xgboost import XGBClassifier


ROOT = Path(__file__).resolve().parent.parent
TRAIN_PATH = ROOT / "data" / "processed" / "train_hybrid_10plus25.pkl"
VAL_PATH = ROOT / "data" / "processed" / "validation_hybrid_10plus25.pkl"
MODELS_DIR = ROOT / "models"
LOG_DIR = ROOT / "logs"
DYNAMIC_PATH = ROOT / "config" / "dynamic_params.json"
FORBIDDEN_ID_COLUMNS = {"Source IP", "Destination IP", "Flow ID", "Timestamp"}


def latest_json(prefix: str) -> Path | None:
    files = sorted(LOG_DIR.glob(f"{prefix}_*.json"))
    return files[-1] if files else None


def compute_sample_weights(y: np.ndarray) -> np.ndarray:
    classes, counts = np.unique(y, return_counts=True)
    total = float(len(y))
    n_classes = float(len(classes))
    cw = {int(c): total / (n_classes * float(cnt)) for c, cnt in zip(classes, counts)}
    return np.array([cw[int(v)] for v in y], dtype=float)


def find_operating_point(
    prob: np.ndarray,
    y_true_multiclass: np.ndarray,
    benign_idx: int,
    precision_floor: float = 0.85,
) -> dict[str, Any]:
    attack_true = (y_true_multiclass != benign_idx).astype(int)
    thresholds = np.arange(0.05, 0.96, 0.01)

    best = {"threshold": 0.5, "precision": 0.0, "recall": 0.0, "f1": -1.0}
    fallback = {"threshold": 0.5, "precision": -1.0, "recall": 0.0, "f1": -1.0}

    attack_prob = prob.copy()
    attack_prob[:, benign_idx] = -1.0
    max_attack_prob = attack_prob.max(axis=1)
    best_attack_cls = attack_prob.argmax(axis=1)

    for t in thresholds:
        pred_mc = np.where(max_attack_prob >= t, best_attack_cls, benign_idx)
        pred_attack = (pred_mc != benign_idx).astype(int)
        p, r, f1, _ = precision_recall_fscore_support(
            attack_true, pred_attack, average="binary", zero_division=0
        )
        current = {"threshold": float(t), "precision": float(p), "recall": float(r), "f1": float(f1)}
        if (p > fallback["precision"]) or (p == fallback["precision"] and r > fallback["recall"]):
            fallback = current
        if p >= precision_floor and r > best["recall"]:
            best = current

    chosen = fallback if best["recall"] <= 0 else best
    pred_mc = np.where(max_attack_prob >= chosen["threshold"], best_attack_cls, benign_idx)
    return {"chosen": chosen, "pred_multiclass": pred_mc}


def per_class_gate(report: dict[str, Any], supports: dict[str, int]) -> dict[str, Any]:
    failed = []
    for cls_name, support in supports.items():
        if cls_name in {"accuracy", "macro avg", "weighted avg"}:
            continue
        cls_report = report.get(cls_name, {})
        recall = float(cls_report.get("recall", 0.0))
        min_required = 0.50 if support >= 1000 else 0.05
        if recall < min_required:
            failed.append(
                {
                    "class": cls_name,
                    "support": int(support),
                    "recall": recall,
                    "required_min_recall": min_required,
                }
            )
    return {"pass": len(failed) == 0, "failed_classes": failed}


def main() -> None:
    if not TRAIN_PATH.exists() or not VAL_PATH.exists():
        raise SystemExit("Hybrid artifacts missing. Run `make build-hybrid` first.")
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    train = pd.read_pickle(TRAIN_PATH)
    val = pd.read_pickle(VAL_PATH)
    label_col = "Label"
    for c in FORBIDDEN_ID_COLUMNS:
        if c in train.columns or c in val.columns:
            raise SystemExit(f"Forbidden ID leakage detected in features: {c}")

    feature_cols = [c for c in train.columns if c not in {label_col, "target_binary"}]
    train_labels = sorted(set(train[label_col].astype(str)))
    val_labels = sorted(set(val[label_col].astype(str)))
    label_to_idx = {lab: i for i, lab in enumerate(train_labels)}
    benign_idx = label_to_idx.get("BENIGN")
    if benign_idx is None:
        raise SystemExit("BENIGN class not found in label set.")

    y_train = train[label_col].astype(str).map(label_to_idx).to_numpy()
    y_val_str = val[label_col].astype(str)
    x_train = train[feature_cols]
    x_val = val[feature_cols]
    sample_weight = compute_sample_weights(y_train)

    model = XGBClassifier(
        n_estimators=700,
        max_depth=8,
        learning_rate=0.06,
        subsample=0.95,
        colsample_bytree=0.9,
        min_child_weight=1,
        reg_lambda=3.0,
        objective="multi:softprob",
        num_class=len(train_labels),
        eval_metric="mlogloss",
        random_state=42,
        n_jobs=6,
        tree_method="hist",
    )
    model.fit(x_train, y_train, sample_weight=sample_weight)
    prob = model.predict_proba(x_val)

    # Any label missing in train label-map is treated as attack for binary metric;
    # per-class report will still show these unseen classes explicitly.
    y_val_binary_ref = (y_val_str != "BENIGN").astype(int).to_numpy()
    y_val_for_gate = np.where(y_val_str.isin(label_to_idx.keys()), y_val_str.map(label_to_idx), benign_idx).astype(int)
    op = find_operating_point(prob, y_val_for_gate, benign_idx=benign_idx, precision_floor=0.85)
    chosen = op["chosen"]
    pred_mc = op["pred_multiclass"]

    inv = {v: k for k, v in label_to_idx.items()}
    pred_label = pd.Series(pred_mc).map(inv)
    all_eval_labels = sorted(set(train_labels).union(set(val_labels)))
    report = classification_report(
        y_val_str,
        pred_label,
        labels=all_eval_labels,
        output_dict=True,
        zero_division=0,
    )
    cm = confusion_matrix(y_val_str, pred_label, labels=all_eval_labels)
    supports = {name: int(report.get(name, {}).get("support", 0)) for name in all_eval_labels}
    gate = per_class_gate(report, supports)

    attack_true = y_val_binary_ref
    attack_pred = (pred_mc != benign_idx).astype(int)
    p, r, f1, _ = precision_recall_fscore_support(
        attack_true, attack_pred, average="binary", zero_division=0
    )

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    model_path = MODELS_DIR / "multiclass_xgb_hybrid.joblib"
    eval_path = LOG_DIR / f"multiclass_eval_{run_ts}.json"
    joblib.dump(
        {
            "model": model,
            "label_to_idx": label_to_idx,
            "feature_cols": feature_cols,
            "benign_idx": benign_idx,
            "threshold": chosen["threshold"],
        },
        model_path,
    )

    # Comparison with latest binary baseline
    binary_path = latest_json("xgb_feature_set_ablation")
    binary_best = None
    if binary_path:
        b = json.loads(binary_path.read_text(encoding="utf-8"))
        results = b.get("results", [])
        combo = [x for x in results if x.get("name") == "combo_targeted10_pca25"]
        binary_best = combo[0] if combo else (results[-1] if results else None)
    if binary_best is None:
        hybrid_eval = latest_json("hybrid_xgb_eval")
        if hybrid_eval:
            binary_best = json.loads(hybrid_eval.read_text(encoding="utf-8"))

    comparison = {}
    if binary_best:
        comparison = {
            "binary_precision_attack": float(binary_best.get("precision_attack", 0.0)),
            "binary_recall_attack": float(binary_best.get("recall_attack", 0.0)),
            "binary_f1_attack": float(binary_best.get("f1_attack", 0.0)),
            "multiclass_precision_attack": float(p),
            "multiclass_recall_attack": float(r),
            "multiclass_f1_attack": float(f1),
            "delta_precision": float(p - float(binary_best.get("precision_attack", 0.0))),
            "delta_recall": float(r - float(binary_best.get("recall_attack", 0.0))),
            "delta_f1": float(f1 - float(binary_best.get("f1_attack", 0.0))),
        }

    payload = {
        "run_timestamp": run_ts,
        "model_path": str(model_path),
        "feature_count": len(feature_cols),
        "class_count_train": len(train_labels),
        "class_count_validation": len(val_labels),
        "unseen_validation_classes": sorted([c for c in val_labels if c not in label_to_idx]),
        "operating_point": chosen,
        "attack_binary_metrics_from_multiclass": {
            "precision_attack": float(p),
            "recall_attack": float(r),
            "f1_attack": float(f1),
        },
        "per_class_report": report,
        "confusion_matrix": cm.tolist(),
        "gate_result": gate,
        "label_to_idx_train": label_to_idx,
        "labels_evaluated": all_eval_labels,
        "comparison_vs_binary": comparison,
    }
    eval_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    comparison_path = LOG_DIR / f"multiclass_vs_binary_{run_ts}.json"
    comparison_payload = {
        "run_timestamp": run_ts,
        "comparison": comparison,
        "recommendation": (
            "use_multiclass_now"
            if comparison and comparison.get("delta_f1", 0.0) >= 0
            else "keep_binary_plus_rules_fallback"
        ),
    }
    comparison_path.write_text(json.dumps(comparison_payload, indent=2), encoding="utf-8")

    dynamic = json.loads(DYNAMIC_PATH.read_text(encoding="utf-8")) if DYNAMIC_PATH.exists() else {}
    dynamic["classification_threshold"] = float(chosen["threshold"])
    dynamic["validation_f1_attack_binary"] = float(f1)
    dynamic["multiclass_model"] = "multiclass_xgb_hybrid"
    dynamic["multiclass_precision_attack"] = float(p)
    dynamic["multiclass_recall_attack"] = float(r)
    DYNAMIC_PATH.write_text(json.dumps(dynamic, indent=2), encoding="utf-8")

    print(json.dumps(payload, indent=2))
    print(f"\nSaved multiclass eval: {eval_path}")
    print(f"Saved comparison report: {comparison_path}")


if __name__ == "__main__":
    main()
