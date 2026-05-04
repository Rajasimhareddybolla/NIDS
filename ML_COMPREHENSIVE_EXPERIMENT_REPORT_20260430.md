# ML Comprehensive Report - CIC-IDS2017 High-Recall Journey

## 1) Purpose

This document explains the full ML journey for the NIDS model in a clear sequence:

- what was tried,
- what numbers were observed,
- what those numbers mean,
- and why the final model choice was made.

The objective is **recall-first intrusion detection** with controlled precision for production streaming.

---

## 2) Data Reality and Initial Problem

### Dataset reality

From project analysis and logs:

- Friday contains concentrated attack families (`DDoS`, `PortScan`, `Bot`) and differs from Mon-Thu distribution.
- A strict Mon-Thu train vs Friday-only validation can behave like unseen-family stress testing, not balanced model tuning.

### Initial performance gap

Early hybrid binary model performance was not sufficient for recall:

- `logs/hybrid_xgb_eval_20260428T182617Z.json`
  - precision (attack): `0.9958`
  - recall (attack): `0.3712`
  - f1 (attack): `0.5408`
  - threshold: `0.30`

**Inference:** the model was very conservative (high precision, low recall), missing too many attacks.

---

## 3) Experiment Flow (What was done, in order)

## Step A - Recall recovery on previous setup

Source: `logs/recall_recovery_experiments_20260428T184030Z.json`

### Threshold sweep on hybrid model

- `t=0.30`: recall `0.4177`, precision `0.9967`
- `t=0.15`: recall `0.4272`, precision `0.9962`
- `t=0.10`: recall `0.4380`, precision `0.9956`
- `t=0.05`: recall `0.4655`, precision `0.9949`

**Inference:** lowering threshold helped recall, but only up to ~`0.4655`; still insufficient.

### scale_pos_weight sweep (at threshold 0.05)

- base `8.39`: recall `0.4655`
- `16.78`: recall `0.4563`
- `50`: recall `0.4172`
- `100`: recall `0.4126`
- `200`: recall `0.3818`

**Inference:** increasing class weight did **not** solve recall; it made recall worse in this setup.

### No-PCA challenger (old split context)

- Recall remained around `0.367` to `0.369` across thresholds.

**Inference:** under the old split context, no-PCA did not win.

---

## Step B - Binary vs multiclass decision

Source: `logs/multiclass_vs_binary_20260428T190400Z.json`

- Binary recall (attack): `0.4655`
- Multiclass-derived recall (attack): `0.3667`
- Delta recall: `-0.0989`
- Recommendation in log: `keep_binary_plus_rules_fallback`

**Inference:** binary objective was better than multiclass for attack recall in this project goal.

---

## Step C - High-recall redesign with grouped splits

### New split artifacts

Source: `logs/recall_splits_20260430T053013Z.json`

- Total rows: `2,830,743`
- Grouped train: `2,073,250`
- Grouped validation: `536,607`
- Friday stress set: `621,371`

Rare class representation exists in grouped sets:

- `Heartbleed` (train `9`, val `2`)
- `Infiltration` (train `29`, val `7`)
- `Web Attack Sql Injection` (train `17`, val `4`)

**Inference:** this split strategy creates better evaluation coverage for rare and day-dependent classes while still preserving Friday stress reporting.

---

## Step D - Candidate training and recall-first selection

Source: `logs/high_recall_xgb_20260430T053442Z.json`

Selection rule:

- maximize recall subject to precision floor (`0.85`),
- then maximize F1.

Candidates:

1. `raw_numeric` (78 features)
2. `targeted10_only` (10 features)
3. `hybrid_10plus25` (35 features)

### Grouped validation results at selected threshold

- **raw_numeric**: precision `0.9900`, recall `0.99996`, f1 `0.99497`, threshold `0.01`
- **targeted10_only**: precision `0.9006`, recall `0.99987`, f1 `0.94766`, threshold `0.01`
- **hybrid_10plus25**: precision `0.9655`, recall `0.99989`, f1 `0.98239`, threshold `0.01`

### Friday stress results at selected threshold

- **raw_numeric**: precision `0.99386`, recall `0.999995`, f1 `0.99692`
- **targeted10_only**: precision `0.95594`, recall `0.999982`, f1 `0.97747`
- **hybrid_10plus25**: precision `0.98490`, recall `0.999991`, f1 `0.99239`

Winner:

- `raw_numeric`
- model path: `models/xgb_high_recall_best.joblib`
- feature space: `raw_numeric`
- threshold: `0.01`

---

## 4) Quantitative Before vs After Summary

### Attack-level binary metrics (key checkpoints)

| Stage | Precision | Recall | F1 |
|---|---:|---:|---:|
| Early hybrid (`t=0.30`) | 0.9958 | 0.3712 | 0.5408 |
| Improved old split hybrid (`t=0.05`) | 0.9949 | 0.4655 | 0.6343 |
| Final grouped high-recall winner (`t=0.01`) | 0.9900 | 0.99996 | 0.99497 |

### Relative lift from early baseline to final winner

- Recall: `0.3712 -> 0.99996` (~`+169%` relative increase)
- F1: `0.5408 -> 0.9950` (~`+84%` relative increase)
- Precision remained very high (`>0.99`) despite recall-first tuning

---

## 5) Per-Class Recall Interpretation

From final high-recall report:

- Friday stress classes:
  - `DDoS`: ~`0.99999`
  - `PortScan`: `1.0`
  - `Bot`: `1.0`
- Grouped rare classes:
  - `Heartbleed`: `1.0` (very small support; treat cautiously)
  - `Infiltration`: `0.857` (raw winner)

**Inference:** major operational attack families now have near-perfect recall. Very low-support classes improved but still require caution due to tiny sample counts.

---

## 6) Qualitative Findings (What we learned)

1. **Threshold choice was the dominant lever** for recall behavior.
2. **More class weight did not equal more recall** in this setup.
3. **Binary objective was better than multiclass** for the target KPI (attack recall).
4. **Split strategy mattered as much as model settings**; grouped split + Friday stress gave better tuning signal.
5. **Raw numeric features beat compressed hybrid features** for this final recall-first objective.

---

## 7) Production Promotion State

Current promoted runtime settings (`config/dynamic_params.json`):

- `classification_threshold`: `0.01`
- `high_recall_precision_attack`: `0.9900253804455679`
- `high_recall_recall_attack`: `0.9999578023461896`
- `high_recall_f1_attack`: `0.9949668039992652`
- `high_recall_model_path`: `models/xgb_high_recall_best.joblib`
- `high_recall_feature_space`: `raw_numeric`

Streaming consumers were updated to load feature space dynamically (raw/hybrid/targeted).

---

## 8) Risks and Caveats

1. **Rare-class uncertainty:** classes with very low support can show unstable metrics.
2. **Low threshold (`0.01`) sensitivity:** excellent recall, but monitor false-positive burden in long live runs.
3. **Dataset shift risk:** future traffic may differ from CIC-IDS2017 distributions.

---

## 9) Operational Inference for Teams

For security operations:

- This model is tuned to **miss almost no attacks** in known-family settings.
- Alert volume governance should be handled via downstream triage/rules and campaign aggregation.

For ML/engineering:

- Keep recall-first KPI with explicit precision floor.
- Preserve grouped + Friday stress evaluation in retraining cycles.
- Track per-family recall drift over time, especially for low-support classes.

---

## 10) Final Recommendation

Use the promoted high-recall binary model as current best:

- `models/xgb_high_recall_best.joblib`
- feature space `raw_numeric`
- threshold `0.01`

Maintain periodic revalidation with:

- grouped validation metrics,
- Friday stress metrics,
- per-class recall dashboard,
- and production false-positive monitoring.
