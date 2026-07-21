# Phase 5 — 研究评价中心 工单板

**Status: accepted**

## Scope
- Pure evaluation helpers under `wtpy/apps/astock/research/`:
  - `regimes.py` — assign_regime / slice_metrics_by_regime
  - `validation.py` — fixed_split / walk_forward_folds / score_in_out
  - `scoring.py` — composite_score / hard_filter / pareto_front / rank_candidates / neighborhood_stability / spike_risk_flag
  - `gua_gain.py` — pair_gua_gain
  - `heatmap.py` — build_heatmap
  - `evaluation.py` — evaluate_trials facade
- Thin API: `POST /api/v1/research/evaluate`
- Gate tests: `tests/apps/astock/test_phase5_gate.py`

## Out of scope
- Optuna / schedules (Phase 6)
- Rework of Phase 4 queue platform

## Acceptance
- `pytest tests/apps/astock/test_phase5_gate.py -q --tb=short` green
- Composite ranking ≠ pure total_return ordering on crafted fixtures
- Pareto multi non-dominated; hard_filter; gua deltas; heatmap shape; spike flag; API ranking
