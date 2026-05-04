from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
import joblib
from sklearn.decomposition import PCA
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
FEATURE_CONTRACT_PATH = ROOT / "config" / "feature_contract.yaml"
DYNAMIC_PATH = ROOT / "config" / "dynamic_params.json"
MODELS_DIR = ROOT / "models"
FORBIDDEN_ID_COLUMNS = {"Source IP", "Destination IP", "Flow ID", "Timestamp"}


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


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML: {path}")
    return data


def read_split(raw_dir: Path, files: list[str]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for name in files:
        p = raw_dir / name
        if not p.exists():
            continue
        df = pd.read_csv(p, low_memory=False)
        df.columns = [c.strip() for c in df.columns]
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def normalize_label(s: pd.Series) -> pd.Series:
    return (
        s.astype(str)
        .str.strip()
        .str.replace("\ufffd", "-", regex=False)
        .str.replace("Web Attack -", "Web Attack", regex=False)
    )


def to_numeric_if_possible(df: pd.DataFrame, exclude: set[str]) -> list[str]:
    numeric_cols: list[str] = []
    for c in df.columns:
        if c in exclude:
            continue
        series = pd.to_numeric(df[c], errors="coerce")
        if series.notna().mean() >= 0.7:
            df[c] = series
            numeric_cols.append(c)
    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    return numeric_cols


def top_corr_pairs(df: pd.DataFrame, top_k: int = 20) -> list[dict[str, Any]]:
    corr = df.corr(numeric_only=True).abs()
    if corr.empty:
        return []
    mask = np.triu(np.ones(corr.shape), k=1).astype(bool)
    upper = corr.where(mask)
    pairs = upper.stack().sort_values(ascending=False).head(top_k)
    return [
        {"feature_a": str(i), "feature_b": str(j), "abs_corr": float(v)}
        for (i, j), v in pairs.items()
    ]


def main() -> None:
    settings = load_yaml(SETTINGS_PATH)
    _ = load_yaml(FEATURE_CONTRACT_PATH)  # reserved for future contract extension
    dynamic = json.loads(DYNAMIC_PATH.read_text(encoding="utf-8")) if DYNAMIC_PATH.exists() else {}

    raw_dir = ROOT / settings["paths"]["raw_data_dir"]
    out_dir = ROOT / settings["paths"]["processed_data_dir"]
    log_dir = ROOT / settings["paths"]["log_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    label_col = str(settings["dataset"]["label_column"]).strip()
    train = read_split(raw_dir, settings["dataset"]["train_files"])
    val = read_split(raw_dir, settings["dataset"]["validation_files"])
    if train.empty or val.empty:
        raise SystemExit("Missing raw train/validation files.")

    train = train.drop_duplicates().copy()
    val = val.drop_duplicates().copy()
    for c in FORBIDDEN_ID_COLUMNS:
        if c in train.columns:
            train = train.drop(columns=[c])
        if c in val.columns:
            val = val.drop(columns=[c])
    train[label_col] = normalize_label(train[label_col])
    val[label_col] = normalize_label(val[label_col])
    train["target_binary"] = (train[label_col] != "BENIGN").astype(int)
    val["target_binary"] = (val[label_col] != "BENIGN").astype(int)

    exclude = {label_col, "target_binary"}
    numeric_train = to_numeric_if_possible(train, exclude)
    _ = to_numeric_if_possible(val, exclude)
    numeric_cols = [c for c in numeric_train if c in val.columns]

    targeted_10 = [c for c in TARGETED_10_CANDIDATES if c in numeric_cols][:10]
    pca_pool = [c for c in numeric_cols if c not in targeted_10]

    pca_k = int(dynamic.get("pca_k", 25))
    pca_k = min(pca_k, len(pca_pool))
    if pca_k <= 0:
        raise SystemExit("No columns available for PCA branch.")

    # PCA branch only: median imputation + scaling required mathematically.
    imputer = SimpleImputer(strategy="median")
    scaler = StandardScaler(with_mean=True, with_std=True)

    x_train_pca = train[pca_pool]
    x_val_pca = val[pca_pool]
    x_train_imp = imputer.fit_transform(x_train_pca)
    x_val_imp = imputer.transform(x_val_pca)
    x_train_scaled = scaler.fit_transform(x_train_imp)
    x_val_scaled = scaler.transform(x_val_imp)

    pca = PCA(n_components=pca_k, random_state=42)
    train_pca = pca.fit_transform(x_train_scaled)
    val_pca = pca.transform(x_val_scaled)
    pca_cols = [f"PCA_{i+1}" for i in range(pca_k)]

    train_targeted = train[targeted_10].copy()
    val_targeted = val[targeted_10].copy()

    train_h = pd.concat(
        [
            train_targeted.reset_index(drop=True),
            pd.DataFrame(train_pca, columns=pca_cols),
            train[[label_col, "target_binary"]].reset_index(drop=True),
        ],
        axis=1,
    )
    val_h = pd.concat(
        [
            val_targeted.reset_index(drop=True),
            pd.DataFrame(val_pca, columns=pca_cols),
            val[[label_col, "target_binary"]].reset_index(drop=True),
        ],
        axis=1,
    )

    # Save artifacts
    train_path = out_dir / "train_hybrid_10plus25.pkl"
    val_path = out_dir / "validation_hybrid_10plus25.pkl"
    train_h.to_pickle(train_path)
    val_h.to_pickle(val_path)

    # Persist hybrid preprocessing so streaming inference (Spark/Python) can
    # reproduce PCA features exactly.
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    preprocess_bundle = {
        "targeted_10": targeted_10,
        "pca_pool": pca_pool,
        "pca_k": pca_k,
        "pca_cols": pca_cols,
        "feature_cols": targeted_10 + pca_cols,
        "forbidden_id_columns_enforced": sorted(FORBIDDEN_ID_COLUMNS),
    }
    joblib.dump(imputer, MODELS_DIR / "hybrid_preprocess_imputer.joblib")
    joblib.dump(scaler, MODELS_DIR / "hybrid_preprocess_scaler.joblib")
    joblib.dump(pca, MODELS_DIR / "hybrid_preprocess_pca.joblib")
    (MODELS_DIR / "hybrid_preprocess_bundle.json").write_text(
        json.dumps(preprocess_bundle, indent=2),
        encoding="utf-8",
    )

    var_retained = float(np.sum(pca.explained_variance_ratio_))
    dynamic["pca_k"] = pca_k
    dynamic["pca_variance_retained"] = var_retained
    DYNAMIC_PATH.write_text(json.dumps(dynamic, indent=2), encoding="utf-8")

    corr_cols = targeted_10 + pca_cols
    corr_pairs = top_corr_pairs(train_h[corr_cols], top_k=25)

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "run_timestamp": run_ts,
        "targeted_feature_count": len(targeted_10),
        "pca_component_count": pca_k,
        "total_model_features": len(targeted_10) + pca_k,
        "targeted_features": targeted_10,
        "pca_source_feature_count": len(pca_pool),
        "pca_variance_retained_at_k": var_retained,
        "train_rows": int(len(train_h)),
        "validation_rows": int(len(val_h)),
        "top_post_transform_correlations_abs": corr_pairs,
        "artifacts": {
            "train_hybrid": str(train_path),
            "validation_hybrid": str(val_path),
            "dynamic_params": str(DYNAMIC_PATH),
        },
        "forbidden_id_columns_enforced": sorted(FORBIDDEN_ID_COLUMNS),
    }
    report_path = log_dir / f"hybrid_feature_space_{run_ts}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report, indent=2))
    print(f"\nSaved report: {report_path}")


if __name__ == "__main__":
    main()
