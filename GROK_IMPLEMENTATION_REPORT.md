# GROK Implementation Report (catalog isolation + 735 provenance audit)

**Date:** 2026-07-19  
**Git:** **no** commit / push / reset / checkout / rebase  
**Network market re-download:** **not performed**

This is **not** “A–G complete.” It records a targeted fix to data catalog isolation, 735 provenance honesty, and refreshed outputs.

---

## 1. Modified files

| File | Role |
|---|---|
| `wtpy/apps/astock/cli.py` | Selection import isolation; formula audit gate; rebuild/confirm/prune CLIs; no name-based provenance |
| `wtpy/apps/astock/config.py` | Storage override rebinds mapping paths |
| `wtpy/apps/astock/data/catalog.py` | **New** rebuild-catalog + `selected_universe_sha` |
| `wtpy/apps/astock/data/io_util.py` | **New** atomic JSON/text writes |
| `wtpy/apps/astock/data/data_store.py` | Atomic manifest; FileManifest restored |
| `wtpy/apps/astock/data/universe.py` | Atomic universe save |
| `wtpy/apps/astock/indicators/tn6_importer.py` | Unconfirmed pair defaults; confirm/prune/resolve audit |
| `wtpy/apps/astock/reports.py` | Remove `summary.xlsx.failed` on success; delete stale xlsx on fail |
| `tests/apps/astock/test_catalog_isolation.py` | **New** |
| `tests/apps/astock/test_formula_provenance.py` | **New** (unconfirmed defaults; confirm; SHA invalidate) |
| `tests/apps/astock/test_e2e.py` | Selection import expectations |
| `GROK_IMPLEMENTATION_REPORT.md` | This file |

---

## 2. Catalog isolation

| Mode | Global `manifest.json` / `universe.json` |
|---|---|
| Full import (no `--codes`) | Updated |
| `--codes` import | **Not** updated; writes `storage/astock/selections/import_sel_*.json` |

`run_meta.repro` now records:

- `global_manifest_sha`, `global_universe_sha`, `global_universe_count`
- `selected_universe_sha`, `selected_codes_count`, `selected_codes`
- (no ambiguous single `manifest_sha` as the only catalog id)

`selected_universe_sha` = SHA256 of sorted unique std codes.

---

## 3. Rebuild catalog (no re-download)

```text
python -m wtpy.apps.astock rebuild-catalog
```

| | Before | After |
|---|---|---|
| manifest count | 10 | **5217** |
| universe count | 10 | **5217** |
| global_manifest_sha | `141a6ee4…` | `8e66032136b992eb4b37dca6b267172e66b626f8f89128cbbd523d254b56b6ce` |
| global_universe_sha | `a5c3d23b…` | `32abcaf1c8da94d0475eee2058f30624166f78243fe5103a115af9fee7e2cd70` |
| csv / npz files | 5217 / 5217 | unchanged |
| factors | not recomputed | not recomputed |

---

## 4. 735 provenance (honest)

| Field | Current production value |
|---|---|
| formula_provenance | **`user_confirmation_required`** |
| source_pair_status | **`paired_unconfirmed`** |
| formal_backtest_allowed | **false** |
| research_backtest_allowed | **true** (only with `--research-unconfirmed-formula`) |

- Removed name-contains-`735` → `user_provided_human_formula`.
- `pair-735` only pairs paths; does **not** confirm.
- Confirmation requires explicit:
  `confirm-indicator-source --tn6 … --source … --confirmed-by … --confirm-user-provided`
- **Not run for user** in this round (per instructions).
- Old `pool10_735_balanced_v2`: keep on disk; treat as **`provenance_unverified`**, not formal.

Source map after `prune-source-map`: **1 valid**, **0 invalid** (pytest temp orphan removed).

---

## 5. Tests

```text
python -m pytest tests/apps/astock -q -rs
82 passed  (see outputs/astock/pytest_final_catalog.txt / latest full run)
```

(Latest focused full run after e2e skip-factors: **82 passed**.)

---

## 6. New outputs (old dirs not overwritten)

| Run | Path | status | price_mode | ret | buys/sells/open |
|---|---|---|---|---:|---|
| V5 formal | `outputs/astock/pool10_bt_v6/` | **ok** | adjusted | **+6.381%** | 44/44/0 |
| V5 research | `outputs/astock/pool10_bt_research_v5/` | **research_unadjusted** | raw | **+5.370%** | 44/44/0 |
| 735 research | `outputs/astock/pool10_735_research_v3/` | **research_unconfirmed_formula** | raw | **+6.582%** | 219/217/2 |

735 sell reasons (research_v3): hold_expired 106, stop_loss 84, take_profit 27.

| Check | Result |
|---|---|
| Formal 735 | **exit 4** `rejected_unconfirmed_formula` |
| Research 735 | **exit 0** with explicit flags |
| `summary.xlsx.failed` in new dirs | **none** |
| code SHA in three run_meta | **`ff2a79936b8387df196b2c9585c3314318036ee97b61d0e80b8eb43413e3171b`** = live `_astock_code_sha()` |
| global_manifest_sha in runs | `8e660321…` matches disk |
| selected_universe_sha (10 names) | `e629f1da80c0c0e98bf6a11083edea7573a7c894c7330957e2fefc3a80fa7c7f` |
| selected_codes_count | 10 |
| global_universe_count | 5217 |

---

## 7. Still not Done

| Item | Status |
|---|---|
| Full-market formal adj PnL 2016–2026 | **Not Done** (day files present; bulk factors not complete; not claimed) |
| MIN60 formal | **No-Go** |
| ET_SEL production | **Partial** |
| Python 3.9 | **Blocked** |
| 双增 tn6 source | **Blocked** |
| TDX golden sample parity | **No-Go** |
| 735 formal | **Blocked** until explicit `confirm-indicator-source` |
| Delist/ST/exact limits | **Partial** |

---

## 8. Git

**No** git commit / push / reset / checkout / rebase.

*End of report. Tests passing ≠ plan complete.*

---

## 735 用户确认与正式回测（会话追加）

**用户指令：** 确认 735，confirmed-by=我

| 字段 | 值 |
|---|---|
| formula_provenance | user_provided_human_formula |
| source_pair_status | paired_confirmed |
| formal_backtest_allowed | true |
| confirmed_by | 我 |
| package_sha256 | bbd591d2e0e4aae24aa26a37ed963665d87332359c290598c84ef48f3bf22144 |
| source_sha256 | d3f513487e4c89df3569461b51ff6afe1e745b9dc34fac6460922d7c99d6638c |
| confirmation_method | cli_confirm_user_provided |

**正式回测输出（未覆盖旧目录）：** outputs/astock/pool10_735_formal_v1/

| 指标 | 值 |
|---|---:|
| status | ok |
| price_mode | adjusted |
| total_return | +15.05% |
| n_buys / n_sells / open | 236 / 234 / 2 |
| sell_reason_counts | hold_expired 122, stop_loss 81, take_profit 31 |
| open_market_value | ~228,417 |
| stop_loss / take_profit | 3% / 8% |
| validate-indicator | exit 0 |

说明：
- 该确认来自用户会话明确授权，不是从 .tn6 逆向得到。
- 通达信黄金样本一致性仍为 **No-Go**。
- 旧的 pool10_735_research_v3（research_unconfirmed_formula）与未确认时期结果仍保留作对照。

---

# CURRENT STATUS (authoritative — post P1/P2 audit fix)

**Date:** 2026-07-19  
**Git:** no commit / push / reset / checkout / rebase  
**Post-run meta patching:** none for formal_v2 / research_adjusted_v4  

## Pytest
Log: outputs/astock/pytest_final_catalog_v2.txt  
Summary: **91 passed in 6.93s** (final clean run after offline fixture fix).

## Catalog
| item | count/sha |
|---|---|
| csv | 5217 |
| npz | 5217 |
| global manifest/universe | 5217 / 5217 |
| global_manifest_sha | 8e66032136b992eb4b37dca6b267172e66b626f8f89128cbbd523d254b56b6ce |
| global_universe_sha | 32abcaf1c8da94d0475eee2058f30624166f78243fe5103a115af9fee7e2cd70 |

## 735 live SHA
| | |
|---|---|
| package | bbd591d2e0e4aae24aa26a37ed963665d87332359c290598c84ef48f3bf22144 |
| source | d3f513487e4c89df3569461b51ff6afe1e745b9dc34fac6460922d7c99d6638c |
| provenance | **user_provided_human_formula** (user confirmed-by=我; not reverse engineered) |
| status | **paired_confirmed** |

## Final artifacts (do not use post-patched v1 as formal)
| path | status | price_mode | note |
|---|---|---|---|
| outputs/astock/pool10_735_formal_v2/ | ok | adjusted | **authoritative formal** |
| outputs/astock/pool10_735_research_adjusted_v4/ | research_unconfirmed_formula | adjusted | proves unconfirmed research ≠ requires raw |
| outputs/astock/pool10_735_formal_v1/ | historical | adjusted | run_meta was post-edited earlier; **not** final acceptance |

formal_v2 metrics: total_return≈+15.05%, buys=236, sells=234, open=2; sell_reasons hold=122 SL=81 TP=31.

Code SHA recorded in formal_v2/research_adjusted_v4: 493f67d619a60fcafc79ddb0d69160eb4694eb0c86bc9e67a51f7caff5d6ae59 (matches generation-time source).

## Still not complete
Full-market formal PnL · MIN60 No-Go · ET_SEL Partial · Python3.9 Blocked · dual-increase source Blocked · TDX golden sample No-Go · delist/ST Partial.

**Tests passing ≠ A–G complete.**

