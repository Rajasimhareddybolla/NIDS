import streamlit as st
import pandas as pd
from pymongo import MongoClient
import os
import plotly.express as px
import plotly.graph_objects as go

st.set_page_config(page_title="NIDS Data Analysis", layout="wide")

@st.cache_resource
def get_db():
    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    client = MongoClient(uri)
    return client["nids_analysis"]

db = get_db()
col_benchmarks = db["benchmarks"]
col_models = db["models"]

st.title("NIDS Evaluation Dashboard 🛡️")
st.markdown("This dashboard analyzes network intrusion data and compares streaming model processing and metrics.")

# 1. Head to Head Benchmark Results
st.header("1. Streaming Benchmark Results (Head-to-Head)")
bench_doc = col_benchmarks.find_one()

if bench_doc and "results" in bench_doc:
    results = bench_doc["results"]
    
    python_eps = results["python"].get("processed_rate_eps", 0)
    python_latency = results["python"].get("latency_ingest_to_write_ms_p95", 0)
    spark_eps = results["spark_v3"].get("processed_rate_eps", 0)
    spark_latency = results["spark_v3"].get("latency_ingest_to_write_ms_p95", 0)

    df_bench = pd.DataFrame([
        {"Consumer": "Python", "Processed EPS": python_eps, "p95 Latency (ms)": python_latency},
        {"Consumer": "Spark v3", "Processed EPS": spark_eps, "p95 Latency (ms)": spark_latency}
    ])
    
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Consumer Throughput")
        fig1 = px.bar(df_bench, x="Consumer", y="Processed EPS", text="Processed EPS", color="Consumer", title="Processed EPS Comparison")
        st.plotly_chart(fig1, use_container_width=True)
        
    with col2:
        st.subheader("p95 Latency")
        fig2 = px.bar(df_bench, x="Consumer", y="p95 Latency (ms)", text="p95 Latency (ms)", color="Consumer", title="Ingest-to-Write p95 Latency")
        st.plotly_chart(fig2, use_container_width=True)
else:
    st.info("No benchmark data found in MongoDB. Make sure to run the seed script first.")

# 2. Model Evaluation Metrics
st.header("2. High Recall XGBoost Evaluation")
model_doc = col_models.find_one()

if model_doc and "candidates" in model_doc:
    candidates = model_doc["candidates"]
    
    model_data = []
    for c in candidates:
        model_data.append({
            "Feature Space": c["name"],
            "Grouped Precision": c["grouped_validation"]["precision"],
            "Grouped Recall": c["grouped_validation"]["recall"],
            "Grouped F1 Score": c["grouped_validation"]["f1"],
            "Feature Count": c["feature_count"]
        })
    df_models = pd.DataFrame(model_data)
    
    st.dataframe(df_models)

    fig3 = go.Figure()
    fig3.add_trace(go.Bar(
        x=df_models["Feature Space"],
        y=df_models["Grouped Precision"],
        name="Precision",
        marker_color="indianred"
    ))
    fig3.add_trace(go.Bar(
        x=df_models["Feature Space"],
        y=df_models["Grouped Recall"],
        name="Recall",
        marker_color="lightsalmon"
    ))
    fig3.add_trace(go.Bar(
        x=df_models["Feature Space"],
        y=df_models["Grouped F1 Score"],
        name="F1 Score",
        marker_color="crimson"
    ))

    fig3.update_layout(barmode="group", title="Model Metrics by Feature Configuration (Grouped Validation)", yaxis=dict(range=[.85, 1.05]))
    st.plotly_chart(fig3, use_container_width=True)

    winner = model_doc.get("winner", "N/A")
    st.success(f"**Selected Winning Model:** `{winner}`")
else:
    st.info("No model evaluation data found in MongoDB.")
