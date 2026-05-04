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
LOG_DIR = ROOT / "logs"
PROCESSED_DIR = ROOT / "data" / "processed"

FORBIDDEN_ID_COLUMNS = {"Source IP", "Destination IP", "Flow ID", "Timestamp"}


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML: {path}")
    return data


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
        s = pd.to_numeric(df[c], errors="coerce")
        if s.notna().mean() >= 0.7:
            df[c] = s
            numeric_cols.append(c)
    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    return numeric_cols


def build_grouped_split(
    df: pd.DataFrame,
    label_col: str,
    val_ratio: float = 0.2,
    seed: int = 42,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    train_parts: list[pd.DataFrame] = []
    val_parts: list[pd.DataFrame] = []

    for (_, _), g in df.groupby([label_col, "__source_file"], dropna=False):
        n = len(g)
        if n <= 1:
            train_parts.append(g)
            continue
        idx = np.arange(n)
        rng.shuffle(idx)
        cut = int(round(n * val_ratio))
        cut = max(1, min(cut, n - 1))
        val_idx = idx[:cut]
        train_idx = idx[cut:]
        train_parts.append(g.iloc[train_idx])
        val_parts.append(g.iloc[val_idx])

    train = pd.concat(train_parts, ignore_index=True) if train_parts else pd.DataFrame()
    val = pd.concat(val_parts, ignore_index=True) if val_parts else pd.DataFrame()
    return train, val


def preprocess_xgb_ready(df: pd.DataFrame, label_col: str) -> pd.DataFrame:
    out = df.copy()
    out = out.drop_duplicates().copy()
    for c in FORBIDDEN_ID_COLUMNS:
        if c in out.columns:
            out.drop(columns=[c], inplace=True)
    out[label_col] = normalize_label(out[label_col])
    out["target_binary"] = (out[label_col] != "BENIGN").astype(int)
    out["target_multiclass"] = pd.Categorical(out[label_col]).codes.astype(int)
    _ = to_numeric_if_possible(out, exclude={label_col, "__source_file", "target_binary", "target_multiclass"})
    return out


def main() -> None:
    settings = load_yaml(SETTINGS_PATH)
    raw_dir = ROOT / settings["paths"]["raw_data_dir"]
    label_col = str(settings["dataset"]["label_column"]).strip()
    train_files = list(settings["dataset"]["train_files"])
    val_files = list(settings["dataset"]["validation_files"])
    all_files = train_files + val_files

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    frames: list[pd.DataFrame] = []
    for f in all_files:
        p = raw_dir / f
        if not p.exists():
            continue
        df = pd.read_csv(p, low_memory=False)
        df.columns = [c.strip() for c in df.columns]
        df["__source_file"] = f
        frames.append(df)
    if not frames:
        raise SystemExit("No CIC-IDS raw CSV files found for split building.")

    full = pd.concat(frames, ignore_index=True)
    full[label_col] = normalize_label(full[label_col])

    is_friday = full["__source_file"].str.contains("Friday", case=False, na=False)
    friday_stress = full[is_friday].copy()
    base_for_split = full.copy()

    grouped_train, grouped_val = build_grouped_split(base_for_split, label_col=label_col, val_ratio=0.2, seed=42)

    train_ready = preprocess_xgb_ready(grouped_train, label_col)
    val_ready = preprocess_xgb_ready(grouped_val, label_col)
    friday_ready = preprocess_xgb_ready(friday_stress, label_col)

    train_path = PROCESSED_DIR / "train_recall_xgb_ready.pkl"
    val_path = PROCESSED_DIR / "validation_recall_xgb_ready.pkl"
    friday_path = PROCESSED_DIR / "friday_stress_xgb_ready.pkl"
    train_ready.to_pickle(train_path)
    val_ready.to_pickle(val_path)
    friday_ready.to_pickle(friday_path)

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "run_timestamp": run_ts,
        "input_files_count": len(all_files),
        "rows_total": int(len(full)),
        "rows_grouped_train": int(len(train_ready)),
        "rows_grouped_validation": int(len(val_ready)),
        "rows_friday_stress": int(len(friday_ready)),
        "label_distribution_train_top20": {k: int(v) for k, v in train_ready[label_col].value_counts().head(20).items()},
        "label_distribution_val_top20": {k: int(v) for k, v in val_ready[label_col].value_counts().head(20).items()},
        "label_distribution_friday_top20": {k: int(v) for k, v in friday_ready[label_col].value_counts().head(20).items()},
        "artifacts": {
            "train_recall_xgb_ready": str(train_path),
            "validation_recall_xgb_ready": str(val_path),
            "friday_stress_xgb_ready": str(friday_path),
        },
    }
    out = LOG_DIR / f"recall_splits_{run_ts}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nSaved split report: {out}")


if __name__ == "__main__":
    main()

