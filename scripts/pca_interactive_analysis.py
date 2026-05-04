from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import yaml
from plotly.subplots import make_subplots
from sklearn.decomposition import PCA
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
PILOT_PATH = ROOT / "data" / "processed" / "pilot_10k.csv"


def load_settings() -> dict[str, Any]:
    with SETTINGS_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def clean_label(values: pd.Series) -> pd.Series:
    return values.astype(str).str.strip().str.replace("\ufffd", "-", regex=False)


def load_data(settings: dict[str, Any]) -> pd.DataFrame:
    if PILOT_PATH.exists():
        df = pd.read_csv(PILOT_PATH, low_memory=False)
        df.columns = [c.strip() for c in df.columns]
        return df

    raise FileNotFoundError("pilot_10k.csv not found. Run `make phase1` first.")


def prep_numeric(df: pd.DataFrame, label_col: str) -> tuple[pd.DataFrame, list[str]]:
    ignore = {label_col, "__source_file"}
    numeric_candidates = []
    for col in df.columns:
        if col in ignore:
            continue
        series = pd.to_numeric(df[col], errors="coerce")
        valid_ratio = series.notna().mean()
        if valid_ratio >= 0.7:
            numeric_candidates.append(col)
            df[col] = series
    numeric_df = df[numeric_candidates].replace([np.inf, -np.inf], np.nan)
    return numeric_df, numeric_candidates


def build_report(
    pca: PCA,
    pca_matrix: np.ndarray,
    numeric_df: pd.DataFrame,
    feature_cols: list[str],
    label_series: pd.Series,
    run_ts: str,
    log_dir: Path,
) -> tuple[Path, Path]:
    explained = pca.explained_variance_ratio_
    cumulative = np.cumsum(explained)
    k95 = int(np.argmax(cumulative >= 0.95) + 1)
    k99 = int(np.argmax(cumulative >= 0.99) + 1)

    comp_df = pd.DataFrame(
        {
            "component": np.arange(1, len(explained) + 1),
            "explained_variance": explained,
            "cumulative_variance": cumulative,
        }
    )

    loadings = pd.DataFrame(
        pca.components_.T,
        index=feature_cols,
        columns=[f"PC{i}" for i in range(1, pca.n_components_ + 1)],
    )
    top_pc1 = loadings["PC1"].abs().sort_values(ascending=False).head(20)
    top_pc2 = loadings["PC2"].abs().sort_values(ascending=False).head(20) if pca.n_components_ >= 2 else top_pc1

    fig = make_subplots(
        rows=2,
        cols=2,
        subplot_titles=(
            "Explained Variance (Scree)",
            "Cumulative Variance",
            "Top |Loadings| PC1",
            "Top |Loadings| PC2",
        ),
        vertical_spacing=0.15,
        horizontal_spacing=0.1,
    )
    fig.add_trace(
        go.Bar(x=comp_df["component"], y=comp_df["explained_variance"], name="Explained"),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=comp_df["component"], y=comp_df["cumulative_variance"], mode="lines+markers", name="Cumulative"),
        row=1,
        col=2,
    )
    fig.add_trace(
        go.Bar(x=top_pc1.index.tolist(), y=top_pc1.values.tolist(), name="PC1 loadings"),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Bar(x=top_pc2.index.tolist(), y=top_pc2.values.tolist(), name="PC2 loadings"),
        row=2,
        col=2,
    )
    fig.add_hline(y=0.95, row=1, col=2)
    fig.update_xaxes(title_text="Component", row=1, col=1)
    fig.update_xaxes(title_text="Component", row=1, col=2)
    fig.update_yaxes(title_text="Variance Ratio", row=1, col=1)
    fig.update_yaxes(title_text="Cumulative Ratio", row=1, col=2)
    fig.update_layout(height=900, width=1400, title_text="PCA Diagnostic Dashboard")

    proj_df = pd.DataFrame(
        {
            "PC1": pca_matrix[:, 0],
            "PC2": pca_matrix[:, 1] if pca_matrix.shape[1] > 1 else np.zeros(len(pca_matrix)),
            "PC3": pca_matrix[:, 2] if pca_matrix.shape[1] > 2 else np.zeros(len(pca_matrix)),
            "Label": label_series,
        }
    )

    fig2d = px.scatter(
        proj_df.sample(min(len(proj_df), 8000), random_state=42),
        x="PC1",
        y="PC2",
        color="Label",
        title="2D PCA Projection by Label (sampled)",
        opacity=0.65,
    )

    fig3d = px.scatter_3d(
        proj_df.sample(min(len(proj_df), 5000), random_state=42),
        x="PC1",
        y="PC2",
        z="PC3",
        color="Label",
        title="3D PCA Projection by Label (sampled)",
        opacity=0.6,
    )

    html_path = log_dir / f"pca_interactive_report_{run_ts}.html"
    with html_path.open("w", encoding="utf-8") as fh:
        fh.write("<html><head><title>PCA Interactive Analysis</title></head><body>")
        fh.write("<h1>PCA Interactive Analysis</h1>")
        fh.write(f"<p>Rows: {len(numeric_df)} | Features: {len(feature_cols)} | k95: {k95} | k99: {k99}</p>")
        fh.write(fig.to_html(full_html=False, include_plotlyjs="cdn"))
        fh.write(fig2d.to_html(full_html=False, include_plotlyjs=False))
        fh.write(fig3d.to_html(full_html=False, include_plotlyjs=False))
        fh.write("</body></html>")

    summary = {
        "rows_used": int(len(numeric_df)),
        "feature_count": int(len(feature_cols)),
        "k_95_variance": k95,
        "k_99_variance": k99,
        "variance_pc1": float(explained[0]) if len(explained) > 0 else 0.0,
        "variance_pc2": float(explained[1]) if len(explained) > 1 else 0.0,
        "top_pc1_features": top_pc1.index.tolist(),
        "top_pc2_features": top_pc2.index.tolist(),
        "html_report_path": str(html_path),
    }
    summary_path = log_dir / f"pca_summary_{run_ts}.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return html_path, summary_path


def main() -> None:
    settings = load_settings()
    label_col = str(settings["dataset"]["label_column"]).strip()
    log_dir = ROOT / settings["paths"]["log_dir"]
    log_dir.mkdir(parents=True, exist_ok=True)

    df = load_data(settings)
    if label_col not in df.columns:
        raise SystemExit(f"Label column `{label_col}` not present in pilot data.")
    labels = clean_label(df[label_col])

    numeric_df, feature_cols = prep_numeric(df, label_col)
    if len(feature_cols) < 3:
        raise SystemExit("Need at least 3 numeric features for meaningful PCA analysis.")

    imputer = SimpleImputer(strategy="median")
    scaler = StandardScaler(with_mean=True, with_std=True)
    x_imputed = imputer.fit_transform(numeric_df)
    x_scaled = scaler.fit_transform(x_imputed)

    pca = PCA(random_state=42)
    pca_matrix = pca.fit_transform(x_scaled)

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    html_path, summary_path = build_report(pca, pca_matrix, numeric_df, feature_cols, labels, run_ts, log_dir)
    print(f"Saved interactive HTML report: {html_path}")
    print(f"Saved PCA summary JSON: {summary_path}")


if __name__ == "__main__":
    main()
