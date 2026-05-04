from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import yaml
from plotly.subplots import make_subplots
from sklearn.decomposition import PCA
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
RAW_DIR = ROOT / "data" / "raw"
LOG_DIR = ROOT / "logs"

PRESETS = [
    ("fast", 400_000),
    ("balanced", 1_200_000),
    ("full", 2_400_000),
]


def load_settings() -> dict:
    with SETTINGS_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def read_all_data(file_names: list[str]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for name in file_names:
        path = RAW_DIR / name
        if not path.exists():
            continue
        df = pd.read_csv(path, low_memory=False)
        df.columns = [c.strip() for c in df.columns]
        frames.append(df)
    if not frames:
        raise FileNotFoundError("No raw CSV files found in data/raw.")
    return pd.concat(frames, ignore_index=True)


def prepare_numeric(df: pd.DataFrame, label_col: str) -> tuple[pd.DataFrame, list[str], pd.Series]:
    labels = df[label_col].astype(str).str.strip().str.replace("\ufffd", "-", regex=False)
    num_cols: list[str] = []
    for col in df.columns:
        if col == label_col:
            continue
        series = pd.to_numeric(df[col], errors="coerce")
        if series.notna().mean() >= 0.7:
            num_cols.append(col)
            df[col] = series
    x = df[num_cols].replace([np.inf, -np.inf], np.nan)
    return x, num_cols, labels


def run_pca(x: pd.DataFrame, n_rows: int, seed: int = 42) -> dict:
    sample_n = min(n_rows, len(x))
    sampled = x.sample(n=sample_n, random_state=seed, replace=False)

    t0 = perf_counter()
    xi = SimpleImputer(strategy="median").fit_transform(sampled)
    t1 = perf_counter()
    xs = StandardScaler(with_mean=True, with_std=True).fit_transform(xi)
    t2 = perf_counter()
    pca = PCA(random_state=seed)
    pca.fit(xs)
    t3 = perf_counter()

    exp = pca.explained_variance_ratio_
    cum = np.cumsum(exp)
    k95 = int(np.argmax(cum >= 0.95) + 1)
    k99 = int(np.argmax(cum >= 0.99) + 1)

    return {
        "rows_used": int(sample_n),
        "impute_s": round(t1 - t0, 3),
        "scale_s": round(t2 - t1, 3),
        "pca_s": round(t3 - t2, 3),
        "total_s": round(t3 - t0, 3),
        "k95": k95,
        "k99": k99,
        "pc1_var": float(exp[0]) if len(exp) else 0.0,
        "pc2_var": float(exp[1]) if len(exp) > 1 else 0.0,
        "pc3_var": float(exp[2]) if len(exp) > 2 else 0.0,
        "cum10": float(cum[9]) if len(cum) >= 10 else float(cum[-1]),
        "cum20": float(cum[19]) if len(cum) >= 20 else float(cum[-1]),
    }


def build_comparison_html(results: list[dict], out_path: Path) -> None:
    labels = [r["preset"] for r in results]

    fig = make_subplots(
        rows=2,
        cols=2,
        subplot_titles=(
            "Runtime by Preset (seconds)",
            "k for Variance Targets",
            "Variance in First Components",
            "Cumulative Variance @10/@20 PCs",
        ),
    )

    fig.add_trace(go.Bar(x=labels, y=[r["total_s"] for r in results], name="total_s"), row=1, col=1)
    fig.add_trace(go.Bar(x=labels, y=[r["pca_s"] for r in results], name="pca_s"), row=1, col=1)

    fig.add_trace(go.Bar(x=labels, y=[r["k95"] for r in results], name="k95"), row=1, col=2)
    fig.add_trace(go.Bar(x=labels, y=[r["k99"] for r in results], name="k99"), row=1, col=2)

    fig.add_trace(go.Scatter(x=labels, y=[r["pc1_var"] for r in results], mode="lines+markers", name="PC1"), row=2, col=1)
    fig.add_trace(go.Scatter(x=labels, y=[r["pc2_var"] for r in results], mode="lines+markers", name="PC2"), row=2, col=1)
    fig.add_trace(go.Scatter(x=labels, y=[r["pc3_var"] for r in results], mode="lines+markers", name="PC3"), row=2, col=1)

    fig.add_trace(go.Scatter(x=labels, y=[r["cum10"] for r in results], mode="lines+markers", name="cum10"), row=2, col=2)
    fig.add_trace(go.Scatter(x=labels, y=[r["cum20"] for r in results], mode="lines+markers", name="cum20"), row=2, col=2)

    fig.update_layout(height=900, width=1400, title="PCA Full Comparison Across Data Scales")

    with out_path.open("w", encoding="utf-8") as fh:
        fh.write("<html><head><title>PCA Full Comparison</title></head><body>")
        fh.write("<h1>PCA Full Comparison</h1>")
        fh.write("<p>Presets: fast=400k, balanced=1.2M, full=2.4M</p>")
        fh.write(fig.to_html(full_html=False, include_plotlyjs="cdn"))
        fh.write("</body></html>")


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    all_files = settings["dataset"]["train_files"] + settings["dataset"]["validation_files"]
    label_col = settings["dataset"]["label_column"]

    df = read_all_data(all_files)
    x, num_cols, _ = prepare_numeric(df, label_col)

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results: list[dict] = []
    for preset, rows in PRESETS:
        result = run_pca(x, rows)
        result["preset"] = preset
        result["rows_requested"] = rows
        result["feature_count"] = len(num_cols)
        results.append(result)
        print(f"{preset}: rows={result['rows_used']} total={result['total_s']}s k95={result['k95']}")

    summary = {
        "timestamp": run_ts,
        "total_rows_available": int(len(x)),
        "feature_count": len(num_cols),
        "results": results,
    }
    json_path = LOG_DIR / f"pca_full_comparison_{run_ts}.json"
    html_path = LOG_DIR / f"pca_full_comparison_{run_ts}.html"
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    build_comparison_html(results, html_path)

    print(f"Saved JSON summary: {json_path}")
    print(f"Saved HTML report: {html_path}")


if __name__ == "__main__":
    main()
