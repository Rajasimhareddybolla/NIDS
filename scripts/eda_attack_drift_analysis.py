from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml


ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML in {path}")
    return data


def normalize_label(s: pd.Series) -> pd.Series:
    return (
        s.astype(str)
        .str.strip()
        .str.replace("\ufffd", "-", regex=False)
        .str.replace("Web Attack -", "Web Attack", regex=False)
    )


def safe_numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out.replace([np.inf, -np.inf], np.nan, inplace=True)
    return out


def effect_size_drift(train_s: pd.Series, val_s: pd.Series) -> dict[str, float]:
    tr = train_s.dropna()
    va = val_s.dropna()
    if tr.empty or va.empty:
        return {"mean_diff": 0.0, "std_pooled": 0.0, "cohens_d": 0.0}
    mean_diff = float(va.mean() - tr.mean())
    pooled = float(np.sqrt((tr.var(ddof=1) + va.var(ddof=1)) / 2.0)) if (len(tr) > 1 and len(va) > 1) else 0.0
    d = float(mean_diff / pooled) if pooled > 0 else 0.0
    return {"mean_diff": mean_diff, "std_pooled": pooled, "cohens_d": d}


def top_corr_pairs(df: pd.DataFrame, top_k: int = 25) -> list[dict[str, float | str]]:
    corr = df.corr(numeric_only=True).abs()
    if corr.empty:
        return []
    mask = np.triu(np.ones(corr.shape), k=1).astype(bool)
    upper = corr.where(mask)
    pairs = (
        upper.stack()
        .sort_values(ascending=False)
        .head(top_k)
    )
    return [
        {"feature_a": str(i), "feature_b": str(j), "abs_corr": float(v)}
        for (i, j), v in pairs.items()
    ]


def main() -> None:
    settings = load_yaml(SETTINGS_PATH)
    raw_dir = ROOT / settings["paths"]["raw_data_dir"]
    log_dir = ROOT / settings["paths"]["log_dir"]
    log_dir.mkdir(parents=True, exist_ok=True)
    label_col = str(settings["dataset"]["label_column"]).strip()

    train_files = settings["dataset"]["train_files"]
    val_files = settings["dataset"]["validation_files"]

    def read_split(files: list[str]) -> pd.DataFrame:
        frames = []
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

    train = read_split(train_files)
    val = read_split(val_files)
    if train.empty or val.empty:
        raise SystemExit("Train/validation raw files not available.")

    train = train.drop_duplicates().copy()
    val = val.drop_duplicates().copy()
    train[label_col] = normalize_label(train[label_col])
    val[label_col] = normalize_label(val[label_col])

    feature_cols: list[str] = []
    for c in train.columns:
        if c == label_col:
            continue
        tr_num = pd.to_numeric(train[c], errors="coerce")
        va_num = pd.to_numeric(val[c], errors="coerce")
        if tr_num.notna().mean() >= 0.7 and va_num.notna().mean() >= 0.7:
            feature_cols.append(c)
            train[c] = tr_num
            val[c] = va_num

    train = safe_numeric(train, feature_cols)
    val = safe_numeric(val, feature_cols)

    # Class distribution
    train_dist = {str(k): int(v) for k, v in train[label_col].value_counts().items()}
    val_dist = {str(k): int(v) for k, v in val[label_col].value_counts().items()}

    # Per-class summary for top classes by volume
    top_classes = list(train[label_col].value_counts().head(8).index)
    class_profiles: dict[str, dict[str, float]] = {}
    key_features = [
        "Flow Duration",
        "Total Fwd Packets",
        "Total Backward Packets",
        "Flow Bytes/s",
        "Flow Packets/s",
        "SYN Flag Count",
        "ACK Flag Count",
        "Bwd Packet Length Max",
    ]
    key_features = [f for f in key_features if f in feature_cols]
    for cls in top_classes:
        sub = train[train[label_col] == cls]
        if sub.empty:
            continue
        class_profiles[str(cls)] = {}
        for f in key_features:
            class_profiles[str(cls)][f"{f}__median"] = float(sub[f].median(skipna=True))
            class_profiles[str(cls)][f"{f}__p95"] = float(sub[f].quantile(0.95))

    # Train-vs-validation drift by Cohen's d
    drift = {}
    for f in feature_cols:
        drift[f] = effect_size_drift(train[f], val[f])
    top_drift = sorted(
        [{"feature": f, **v} for f, v in drift.items()],
        key=lambda x: abs(x["cohens_d"]),
        reverse=True,
    )[:25]

    # Correlation clusters (strong pairs)
    corr_pairs = top_corr_pairs(train[feature_cols], top_k=30)

    # Attack vs benign separability using median ratio
    train_binary = train.copy()
    train_binary["__attack"] = (train_binary[label_col] != "BENIGN").astype(int)
    sep = []
    atk = train_binary[train_binary["__attack"] == 1]
    ben = train_binary[train_binary["__attack"] == 0]
    for f in feature_cols:
        a_med = float(atk[f].median(skipna=True)) if not atk.empty else 0.0
        b_med = float(ben[f].median(skipna=True)) if not ben.empty else 0.0
        denom = abs(b_med) + 1e-9
        ratio = float(abs(a_med - b_med) / denom) if denom > 0 else 0.0
        sep.append({"feature": f, "attack_median": a_med, "benign_median": b_med, "relative_gap": ratio})
    sep_top = sorted(sep, key=lambda x: x["relative_gap"], reverse=True)[:25]

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = {
        "run_timestamp": run_ts,
        "train_rows": int(len(train)),
        "validation_rows": int(len(val)),
        "feature_count_numeric": int(len(feature_cols)),
        "top_train_classes": train_dist,
        "top_validation_classes": val_dist,
        "class_profiles_median_p95": class_profiles,
        "top_drift_features_cohens_d": top_drift,
        "top_correlation_pairs_abs": corr_pairs,
        "top_attack_benign_separation_features": sep_top,
    }

    out_path = log_dir / f"eda_attack_drift_{run_ts}.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"\nSaved EDA report: {out_path}")


if __name__ == "__main__":
    main()
