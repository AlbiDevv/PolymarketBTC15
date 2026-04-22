# Frozen report — template

**Report ID:** `FR-{YYYYMMDD}-{short_hash}`  
**Frozen at (UTC):**  
**Author:**

---

## Versions

| Field | Value |
|--------|--------|
| dataset_version | |
| feature_version | |
| split_version | |
| model_version | |
| code_git_sha | |

---

## Data

- Source(s): historical / paper / mixed  
- Row count (train / val / hold-out):  
- Inclusion/exclusion rules (link to spec):  
- Share of **`complement_fallback`** NO rows (from `p_market_source` / quality flags):  
- If fallback share is high: document risk that baseline leans on inferred NO prices, not native books.

---

## Execution / cost assumptions (research EV proxy)

- **Module / version:** `research/cost_assumptions.py` — `COST_ASSUMPTIONS_VERSION` (e.g. `ev_proxy_v1`).  
- **Flat fee per unit (notional):** (default aligned with strategy fee for comparability).  
- **Formula (reported in `evaluate`):** `ev_proxy_i = y_i - p_i - flat_fee` on calibrated / market probabilities.  
- **What this is NOT:** realized PnL from PaperBroker/Live; no bid-ask crossing, no per-fill slippage model in this metric.  
- **Limitations:** state explicitly what remains a proxy until full execution-level backtest is wired.

---

## Split policy

- Train end:  
- Validation end:  
- Hold-out end:  
- Rationale (no shuffle, temporal only):  

---

## Baselines A–E

| Model | Key parameters | Notes |
|--------|------------------|--------|
| A baseline_market | — | |
| B baseline_calibrated | calibration method + params | |
| C baseline_h2_only | H2 zones | |
| D baseline_h4_only | tail thresholds | |
| E model_v1 | full stack + micro cap | |

---

## Metrics (hold-out)

| Metric | A | B | C | D | E |
|--------|---|---|---|---|---|
| EV (after costs) | | | | | |
| Avg PnL / trade | | | | | |
| Brier | | | | | |
| ECE | | | | | |
| Sharpe | | | | | |
| Max DD | | | | | |
| n trades | | | | | |

### By segment (hold-out)

- category  
- tte_bucket  
- liquidity_bucket  
- round-zone vs non-round  
- tail vs non-tail  
- **`p_market_source`:** clean native (`native_yes` + `native_no`) vs `complement_fallback` vs all — metrics must be reported separately in auto-generated `evaluate` JSON (`segments_*`).

---

## Bootstrap

- EV 95% CI:  
- Avg PnL 95% CI:  
- Method (trade-level / day-level):  

---

## Walk-forward summary

- Windows:  
- Stability note:  

---

## Decision

- [ ] **GO** — continue 7d dry / 2w paper on fixed model_version  
- [ ] **NO-GO** — do not scale; revise data/model  

**Reason (one paragraph):**

---

## Anti-leakage attestation

- [ ] Hold-out not used for any parameter tuning  
- [ ] No post-hoc relabeling after viewing hold-out  
- [ ] Target and p_market definitions match `research/definitions.py`  

---

## Change policy after freeze

Any change to dataset, features, or model requires a **new** `*_version` and a **new** report ID.
