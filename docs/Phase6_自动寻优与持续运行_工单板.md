# Phase 6 — 自动寻优与持续运行 工单板

**Status: accepted** (after gate green)

## Scope
- Search / optimizer under `wtpy/apps/astock/research/`:
  - `optimizer.py` — grid_search / random_search / staged_search / optional optuna_search
  - `promote.py` — select_for_full_retest
  - `schedules.py` — daily / nightly / weekend + ScheduleRunner
  - `drift.py` — detect_drift
  - `reports_auto.py` — build_research_summary / mark_paper_candidates
  - `continuous.py` — run_budgeted_search / run_scheduled_research
- API:
  - `POST /api/v1/research/search`
  - `GET /api/v1/research/schedules`
  - `POST /api/v1/research/drift`
  - `POST /api/v1/research/summary`
- Gate tests: `tests/apps/astock/test_phase6_gate.py`

## Out of scope
- Real Celery beat / distributed workers
- Live trading promotion
- Requiring optuna at install time

## Acceptance
- `pytest tests/apps/astock/test_phase6_gate.py -q --tb=short` green
- Seeded grid_search reproducible; random/staged respect budget
- Schedules include daily/nightly/weekend
- Drift flags significant decay; paper flags set; API search/schedules/drift OK
