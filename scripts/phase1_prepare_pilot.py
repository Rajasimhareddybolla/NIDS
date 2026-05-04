from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from subprocess import run
from typing import Any

import numpy as np
import pandas as pd
import yaml


ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
CONTRACT_PATH = ROOT / "config" / "feature_contract.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping in {path}")
    return data


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [col.strip() for col in df.columns]
    return df


def _read_existing(files: list[str], raw_dir: Path) -> tuple[pd.DataFrame, list[str]]:
    frames: list[pd.DataFrame] = []
    missing: list[str] = []

    for file_name in files:
        csv_path = raw_dir / file_name
        if not csv_path.exists():
            missing.append(file_name)
            continue
        frame = pd.read_csv(csv_path, low_memory=False)
        frame = _normalize_columns(frame)
        frame["__source_file"] = file_name
        frames.append(frame)

    if not frames:
        return pd.DataFrame(), missing

    return pd.concat(frames, ignore_index=True), missing


def _build_pilot_sample(df: pd.DataFrame, sample_rows: int, label_col: str) -> pd.DataFrame:
    # Deterministic class-aware sampling for a meaningful pilot:
    # keep BENIGN majority but guarantee attack representation.
    if label_col not in df.columns:
        return df.head(sample_rows).reset_index(drop=True).copy()

    label_series = df[label_col].astype(str).str.strip()
    benign_mask = label_series == "BENIGN"
    benign_df = df[benign_mask]
    attack_df = df[~benign_mask]

    benign_target = min(int(sample_rows * 0.6), len(benign_df))
    attack_target_total = max(sample_rows - benign_target, 0)

    pieces: list[pd.DataFrame] = []
    if benign_target > 0:
        pieces.append(benign_df.head(benign_target))

    if attack_target_total > 0 and not attack_df.empty:
        attack_labels = attack_df[label_col].astype(str).str.strip().unique().tolist()
        per_attack = max(attack_target_total // max(len(attack_labels), 1), 1)
        picked = 0
        for attack_label in attack_labels:
            sub = attack_df[attack_df[label_col].astype(str).str.strip() == attack_label]
            take = min(per_attack, len(sub))
            if take > 0:
                pieces.append(sub.head(take))
                picked += take
        if picked < attack_target_total:
            extra = attack_df.loc[~attack_df.index.isin(pd.concat(pieces, axis=0).index)]
            if not extra.empty:
                pieces.append(extra.head(attack_target_total - picked))

    pilot = pd.concat(pieces, axis=0) if pieces else df.head(sample_rows)
    if len(pilot) < sample_rows:
        remainder = df.loc[~df.index.isin(pilot.index)]
        if not remainder.empty:
            pilot = pd.concat([pilot, remainder.head(sample_rows - len(pilot))], axis=0)
    return pilot.head(sample_rows).reset_index(drop=True).copy()


def _coerce_numeric(df: pd.DataFrame, columns: list[str]) -> dict[str, int]:
    changed_nulls: dict[str, int] = {}
    for col in columns:
        if col not in df.columns:
            continue
        before = int(df[col].isna().sum())
        df[col] = pd.to_numeric(df[col], errors="coerce")
        after = int(df[col].isna().sum())
        changed_nulls[col] = max(after - before, 0)
    return changed_nulls


def _label_distribution(df: pd.DataFrame, label_col: str) -> dict[str, int]:
    if label_col not in df.columns:
        return {}
    cleaned = df[label_col].astype(str).str.strip()
    return {str(k): int(v) for k, v in cleaned.value_counts(dropna=False).items()}


def _duplicate_count(df: pd.DataFrame) -> int:
    return int(df.duplicated().sum())


def _timestamp_parse_stats(df: pd.DataFrame, ts_col: str) -> dict[str, int]:
    if ts_col not in df.columns:
        return {"parsed": 0, "failed": 0}
    parsed = pd.to_datetime(df[ts_col], errors="coerce", utc=True)
    ok = int(parsed.notna().sum())
    fail = int(parsed.isna().sum())
    df[ts_col] = parsed
    return {"parsed": ok, "failed": fail}


def main() -> None:
    settings = _load_yaml(SETTINGS_PATH)
    contract = _load_yaml(CONTRACT_PATH)

    raw_dir = ROOT / settings["paths"]["raw_data_dir"]
    processed_dir = ROOT / settings["paths"]["processed_data_dir"]
    log_dir = ROOT / settings["paths"]["log_dir"]
    sample_rows = int(settings["dataset"]["sample_rows"])
    label_col = str(settings["dataset"]["label_column"]).strip()
    ts_col = str(settings["dataset"]["timestamp_column"]).strip()

    processed_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    train_files = settings["dataset"]["train_files"]
    val_files = settings["dataset"]["validation_files"]
    all_files = train_files + val_files

    df, missing_files = _read_existing(all_files, raw_dir)

    # Zero-manual workflow: try auto-bootstrap when no source files exist.
    if df.empty:
        bootstrap_script = ROOT / "scripts" / "bootstrap_dataset.py"
        if bootstrap_script.exists():
            print("No local CSVs found. Attempting KaggleHub auto-bootstrap...")
            run(["python3", str(bootstrap_script)], check=False)
            df, missing_files = _read_existing(all_files, raw_dir)
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"phase1_data_quality_{run_ts}.json"

    if df.empty:
        report = {
            "status": "no_data",
            "message": "No CIC-IDS CSV files found in data/raw, and auto-bootstrap did not resolve it.",
            "expected_files": all_files,
            "missing_files": missing_files,
            "raw_data_dir": str(raw_dir),
        }
        log_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        print(f"\nSaved report to: {log_path}")
        return

    required_columns = [str(c).strip() for c in contract["required_columns"]]
    missing_columns = [col for col in required_columns if col not in df.columns]

    optional_columns = [str(c).strip() for c in contract.get("optional_columns", [])]

    timestamp_stats = {"parsed": 0, "failed": 0}
    if ts_col in df.columns:
        timestamp_stats = _timestamp_parse_stats(df, ts_col)
        df = df.sort_values(ts_col, ascending=True, na_position="last")

    numeric_candidates = [str(c).strip() for c in contract["numeric_feature_candidates"]]
    numeric_cast_null_increase = _coerce_numeric(df, numeric_candidates)

    inf_count_before = int(np.isinf(df.select_dtypes(include=[np.number]).to_numpy()).sum())
    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    inf_count_after = int(np.isinf(df.select_dtypes(include=[np.number]).to_numpy()).sum())

    pilot_df = _build_pilot_sample(df, sample_rows, label_col)
    pilot_path = processed_dir / "pilot_10k.csv"
    pilot_df.to_csv(pilot_path, index=False)

    report = {
        "status": "ok",
        "raw_rows": int(len(df)),
        "pilot_rows": int(len(pilot_df)),
        "sample_rows_target": sample_rows,
        "input_files_missing": missing_files,
        "columns_total": int(len(df.columns)),
        "required_columns_missing": missing_columns,
        "optional_columns_missing": [c for c in optional_columns if c not in df.columns],
        "timestamp_stats": timestamp_stats,
        "label_distribution_raw": _label_distribution(df, label_col),
        "label_distribution_pilot": _label_distribution(pilot_df, label_col),
        "duplicate_rows_raw": _duplicate_count(df),
        "numeric_cast_null_increase": numeric_cast_null_increase,
        "infinite_values_before_replace": inf_count_before,
        "infinite_values_after_replace": inf_count_after,
        "output_pilot_csv": str(pilot_path),
        "feature_contract_path": str(CONTRACT_PATH),
    }

    log_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nSaved report to: {log_path}")


if __name__ == "__main__":
    main()
