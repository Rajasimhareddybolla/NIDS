from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import yaml
from sklearn.compose import ColumnTransformer
from sklearn.decomposition import PCA
from sklearn.impute import SimpleImputer
from sklearn.metrics import classification_report, precision_recall_fscore_support
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier


ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
CONTRACT_PATH = ROOT / "config" / "feature_contract.yaml"


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {path}")
    return data


def read_files(raw_dir: Path, file_names: list[str]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for name in file_names:
        p = raw_dir / name
        if not p.exists():
            continue
        df = pd.read_csv(p, low_memory=False)
        df.columns = [c.strip() for c in df.columns]
        df["__source_file"] = name
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def clean_label(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.replace("\ufffd", "-", regex=False)


def sample_for_phase2(df: pd.DataFrame, total_rows: int, label_col: str) -> pd.DataFrame:
    labels = clean_label(df[label_col]) if label_col in df.columns else pd.Series([], dtype=str)
    if labels.empty:
        return df.head(total_rows).reset_index(drop=True).copy()

    benign_mask = labels == "BENIGN"
    benign_df = df[benign_mask]
    attack_df = df[~benign_mask]

    benign_target = min(int(total_rows * 0.6), len(benign_df))
    attack_target = max(total_rows - benign_target, 0)

    picks: list[pd.DataFrame] = []
    if benign_target > 0:
        picks.append(benign_df.sample(n=benign_target, random_state=42, replace=False))
    if attack_target > 0 and not attack_df.empty:
        take = min(attack_target, len(attack_df))
        picks.append(attack_df.sample(n=take, random_state=42, replace=False))

    out = pd.concat(picks, axis=0) if picks else df.head(total_rows)
    if len(out) < total_rows:
        extra = df.loc[~df.index.isin(out.index)]
        if not extra.empty:
            out = pd.concat([out, extra.head(total_rows - len(out))], axis=0)
    return out.head(total_rows).reset_index(drop=True).copy()


def main() -> None:
    settings = load_yaml(SETTINGS_PATH)
    contract = load_yaml(CONTRACT_PATH)

    raw_dir = ROOT / settings["paths"]["raw_data_dir"]
    model_dir = ROOT / settings["paths"]["model_dir"]
    log_dir = ROOT / settings["paths"]["log_dir"]
    dynamic_params_path = ROOT / settings["paths"]["dynamic_params"]
    sample_rows = int(settings["dataset"]["sample_rows"])
    label_col = str(settings["dataset"]["label_column"]).strip()

    model_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    train_df = read_files(raw_dir, settings["dataset"]["train_files"])
    val_df = read_files(raw_dir, settings["dataset"]["validation_files"])
    if train_df.empty or val_df.empty:
        raise SystemExit("Train or validation files are missing/empty. Run phase1-auto first.")

    train_df = sample_for_phase2(train_df, int(sample_rows * 0.7), label_col)
    val_df = sample_for_phase2(val_df, int(sample_rows * 0.3), label_col)

    train_df[label_col] = clean_label(train_df[label_col])
    val_df[label_col] = clean_label(val_df[label_col])
    train_df["is_attack"] = (train_df[label_col] != "BENIGN").astype(int)
    val_df["is_attack"] = (val_df[label_col] != "BENIGN").astype(int)

    required = [str(c).strip() for c in contract["required_columns"] if str(c).strip() != label_col]
    feature_cols = [c for c in required if c in train_df.columns and c in val_df.columns]
    if not feature_cols:
        raise SystemExit("No common feature columns found for training.")

    for col in feature_cols:
        train_df[col] = pd.to_numeric(train_df[col], errors="coerce")
        val_df[col] = pd.to_numeric(val_df[col], errors="coerce")
    train_df.replace([np.inf, -np.inf], np.nan, inplace=True)
    val_df.replace([np.inf, -np.inf], np.nan, inplace=True)

    benign = int((train_df["is_attack"] == 0).sum())
    attack = int((train_df["is_attack"] == 1).sum())
    scale_pos_weight = float(benign / max(attack, 1))

    full_feature_pipeline = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler(with_mean=True, with_std=True)),
            ("pca_full", PCA(random_state=42)),
        ]
    )
    full_feature_pipeline.fit(train_df[feature_cols])
    explained = full_feature_pipeline.named_steps["pca_full"].explained_variance_ratio_
    cum = np.cumsum(explained)
    pca_k = int(np.argmax(cum >= 0.95) + 1)
    pca_var = float(cum[pca_k - 1])

    preprocessor = ColumnTransformer(
        transformers=[
            (
                "num",
                Pipeline(
                    steps=[
                        ("imputer", SimpleImputer(strategy="median")),
                        ("scaler", StandardScaler(with_mean=True, with_std=True)),
                        ("pca", PCA(n_components=pca_k, random_state=42)),
                    ]
                ),
                feature_cols,
            )
        ]
    )

    model = XGBClassifier(
        n_estimators=180,
        max_depth=6,
        learning_rate=0.08,
        subsample=0.9,
        colsample_bytree=0.9,
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=42,
        n_jobs=4,
        scale_pos_weight=scale_pos_weight,
    )
    clf = Pipeline(steps=[("pre", preprocessor), ("model", model)])
    clf.fit(train_df[feature_cols], train_df["is_attack"])

    val_proba = clf.predict_proba(val_df[feature_cols])[:, 1]
    y_true = val_df["is_attack"].to_numpy()
    thresholds = np.arange(
        float(settings["threshold_tuning"]["threshold_min"]),
        float(settings["threshold_tuning"]["threshold_max"]),
        float(settings["threshold_tuning"]["threshold_step"]),
    )
    precision_constraint = float(settings["threshold_tuning"]["precision_constraint"])
    best_t = 0.5
    best_f1 = -1.0
    fallback_t = 0.5
    fallback_precision = -1.0
    fallback_f1 = -1.0
    for t in thresholds:
        y_pred = (val_proba >= t).astype(int)
        p, r, f1, _ = precision_recall_fscore_support(y_true, y_pred, average="binary", zero_division=0)
        if (p > fallback_precision) or (p == fallback_precision and f1 > fallback_f1):
            fallback_precision = float(p)
            fallback_f1 = float(f1)
            fallback_t = float(t)
        if p >= precision_constraint and f1 > best_f1:
            best_t = float(t)
            best_f1 = float(f1)
    if best_f1 < 0:
        best_t = fallback_t
        y_pred = (val_proba >= best_t).astype(int)
        p, r, f1, _ = precision_recall_fscore_support(y_true, y_pred, average="binary", zero_division=0)
        best_f1 = float(f1)
    else:
        y_pred = (val_proba >= best_t).astype(int)
        p, r, f1, _ = precision_recall_fscore_support(y_true, y_pred, average="binary", zero_division=0)

    report = classification_report(y_true, y_pred, output_dict=True, zero_division=0)
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    eval_path = log_dir / f"phase2_eval_{run_ts}.json"
    model_path = model_dir / "phase2_xgb_pipeline.joblib"

    eval_payload = {
        "train_rows": int(len(train_df)),
        "validation_rows": int(len(val_df)),
        "feature_count": int(len(feature_cols)),
        "scale_pos_weight": scale_pos_weight,
        "pca_k": pca_k,
        "pca_variance_retained": pca_var,
        "threshold": best_t,
        "precision_attack": float(p),
        "recall_attack": float(r),
        "f1_attack": float(f1),
        "classification_report": report,
        "model_path": str(model_path),
    }
    eval_path.write_text(json.dumps(eval_payload, indent=2), encoding="utf-8")
    joblib.dump(clf, model_path)

    params = {}
    if dynamic_params_path.exists():
        params = json.loads(dynamic_params_path.read_text(encoding="utf-8"))
    params.update(
        {
            "scale_pos_weight": scale_pos_weight,
            "pca_k": pca_k,
            "pca_variance_retained": pca_var,
            "classification_threshold": best_t,
            "validation_f1_attack_binary": float(f1),
        }
    )
    dynamic_params_path.write_text(json.dumps(params, indent=2), encoding="utf-8")

    print(json.dumps(eval_payload, indent=2))
    print(f"\nUpdated dynamic params: {dynamic_params_path}")
    print(f"Saved evaluation report: {eval_path}")
    print(f"Saved model: {model_path}")


if __name__ == "__main__":
    main()
