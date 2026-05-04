from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from sklearn.decomposition import PCA
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
DYNAMIC_PARAMS_PATH = ROOT / "config" / "dynamic_params.json"
FORBIDDEN_ID_COLUMNS = {
    "Source IP",
    "Destination IP",
    "Flow ID",
    "Timestamp",
}


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML: {path}")
    return data


def read_split(raw_dir: Path, files: list[str]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for name in files:
        path = raw_dir / name
        if not path.exists():
            continue
        df = pd.read_csv(path, low_memory=False)
        df.columns = [c.strip() for c in df.columns]
        df["__source_file"] = name
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def normalize_labels(df: pd.DataFrame, label_col: str) -> None:
    df[label_col] = (
        df[label_col]
        .astype(str)
        .str.strip()
        .str.replace("\ufffd", "-", regex=False)
        .str.replace("Web Attack -", "Web Attack", regex=False)
    )


def preprocess_for_xgb(df: pd.DataFrame, label_col: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    original_rows = len(df)
    df = df.drop_duplicates().copy()
    dropped_duplicates = original_rows - len(df)

    normalize_labels(df, label_col)

    removed_id_columns = [c for c in df.columns if c in FORBIDDEN_ID_COLUMNS]
    if removed_id_columns:
        df = df.drop(columns=removed_id_columns)

    # Required target encodings
    df["target_binary"] = (df[label_col] != "BENIGN").astype(int)
    df["target_multiclass"] = pd.Categorical(df[label_col]).codes.astype(int)

    numeric_cols: list[str] = []
    categorical_cols: list[str] = []
    excluded = {label_col, "__source_file", "target_binary", "target_multiclass"}

    for col in df.columns:
        if col in excluded:
            continue
        coerced = pd.to_numeric(df[col], errors="coerce")
        if coerced.notna().mean() >= 0.7:
            df[col] = coerced
            numeric_cols.append(col)
        else:
            df[col] = df[col].astype("category")
            categorical_cols.append(col)

    # Keep NaNs as-is; XGBoost handles missing values natively.
    df.replace([np.inf, -np.inf], np.nan, inplace=True)

    outlier_summary = {}
    for col in numeric_cols[:30]:
        s = df[col].dropna()
        if s.empty:
            continue
        q1 = s.quantile(0.25)
        q3 = s.quantile(0.75)
        iqr = q3 - q1
        if iqr == 0:
            outlier_summary[col] = {"outlier_ratio": 0.0}
            continue
        low = q1 - 1.5 * iqr
        high = q3 + 1.5 * iqr
        ratio = float(((s < low) | (s > high)).mean())
        outlier_summary[col] = {"outlier_ratio": ratio}

    stats = {
        "rows_after_dedup": int(len(df)),
        "dropped_duplicates": int(dropped_duplicates),
        "numeric_feature_count": int(len(numeric_cols)),
        "categorical_feature_count": int(len(categorical_cols)),
        "missing_ratio_top10": {
            k: float(v)
            for k, v in df.isna().mean().sort_values(ascending=False).head(10).items()
        },
        "binary_target_distribution": {
            str(k): int(v) for k, v in df["target_binary"].value_counts().sort_index().items()
        },
        "label_distribution_top15": {
            str(k): int(v) for k, v in df[label_col].value_counts().head(15).items()
        },
        "outlier_ratio_sample": outlier_summary,
        "forbidden_id_columns_removed": removed_id_columns,
    }
    return df, stats


def estimate_pca_25_variance(df: pd.DataFrame, label_col: str) -> float:
    # Analysis-only estimate for the selected pca_k=25.
    numeric_cols = []
    for col in df.columns:
        if col in {label_col, "__source_file", "target_binary", "target_multiclass"}:
            continue
        if pd.api.types.is_numeric_dtype(df[col]):
            numeric_cols.append(col)
    if len(numeric_cols) < 25:
        return 0.0

    sample_n = min(len(df), 1_200_000)
    sampled = df[numeric_cols].sample(n=sample_n, random_state=42, replace=False)
    xi = SimpleImputer(strategy="median").fit_transform(sampled)
    xs = StandardScaler(with_mean=True, with_std=True).fit_transform(xi)
    pca = PCA(random_state=42).fit(xs)
    cumulative = np.cumsum(pca.explained_variance_ratio_)
    return float(cumulative[24])  # k=25 => index 24


def main() -> None:
    settings = load_yaml(SETTINGS_PATH)
    raw_dir = ROOT / settings["paths"]["raw_data_dir"]
    processed_dir = ROOT / settings["paths"]["processed_data_dir"]
    log_dir = ROOT / settings["paths"]["log_dir"]
    label_col = str(settings["dataset"]["label_column"]).strip()

    processed_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    train = read_split(raw_dir, settings["dataset"]["train_files"])
    val = read_split(raw_dir, settings["dataset"]["validation_files"])
    if train.empty or val.empty:
        raise SystemExit("Missing train/validation data in data/raw. Run make phase1-auto first.")

    train_p, train_stats = preprocess_for_xgb(train, label_col)
    val_p, val_stats = preprocess_for_xgb(val, label_col)

    train_path = processed_dir / "train_xgb_ready.pkl"
    val_path = processed_dir / "validation_xgb_ready.pkl"
    train_p.to_pickle(train_path)
    val_p.to_pickle(val_path)

    pca_var_25 = estimate_pca_25_variance(pd.concat([train_p, val_p], ignore_index=True), label_col)
    dynamic = {}
    if DYNAMIC_PARAMS_PATH.exists():
        dynamic = json.loads(DYNAMIC_PARAMS_PATH.read_text(encoding="utf-8"))
    dynamic["pca_k"] = 25
    if pca_var_25 > 0:
        dynamic["pca_variance_retained"] = pca_var_25
    DYNAMIC_PARAMS_PATH.write_text(json.dumps(dynamic, indent=2), encoding="utf-8")

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "run_timestamp": run_ts,
        "policy": {
            "imputation": "not applied (XGBoost handles missing values)",
            "scaling": "not applied (tree model)",
            "categorical_handling": "kept as pandas category where non-numeric",
            "target_encoding": "binary + multiclass numeric targets generated",
            "pca_k_locked": 25,
        },
        "train": train_stats,
        "validation": val_stats,
        "artifacts": {
            "train_pickle": str(train_path),
            "validation_pickle": str(val_path),
            "dynamic_params": str(DYNAMIC_PARAMS_PATH),
        },
    }

    report_path = log_dir / f"data_analysis_preprocess_xgb_{run_ts}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nSaved report: {report_path}")


if __name__ == "__main__":
    main()
