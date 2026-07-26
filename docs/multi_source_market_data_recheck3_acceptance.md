# 多行情源改造 — 第三轮（P1 残留）独立复核

**复核日期**: 2026-07-26  
**角色**: 独立代码审查 / 验收测试（不修改正式代码、不 commit/push）  
**对照**: `docs/multi_source_market_data_recheck2_acceptance.md`（CONDITIONAL PASS，P1 soft-fail / weekly / sync 符号等）

---

## 1. 最终结论

### **CONDITIONAL PASS — 维持有条件通过（P1 核心项已关闭）**

| 项 | 结果 |
|----|------|
| 全量测试 | **538 passed, 1 skipped, 0 failed**（~83s） |
| 双源 hard-fail | **关闭**（代码 + `test_dual_source_fails_without_ready_datasets`） |
| 双源锁定 dataset_id | **关闭**（有 ready 时写入；测试断言 `None not in ds_ids`） |
| weekly_bar_mode 传入引擎 | **关闭（参数贯通）**；`vendor_native` 完整取原生周线仍依赖 `vendor_weekly_bars` 数据注入（P2） |
| sync symbol 标准化 | **关闭**（`_normalize_symbol` + store/SymbolRecord） |
| Live / 100 股差异 | **仍未验证**（需真实环境） |
| P0 | **0** |

**正式 PASS 仍不可给**：缺 live 与真实双源/规模证据。  
**合并**：功能分支可继续；主线需附 CONDITIONAL 与 live 门禁说明。

---

## 2. 本轮声明修复 vs 独立证据

### 2.1 双源创建 soft-fail → hard-fail — **FIXED**

`experiments.py` ~1002–1026：

- 对 tdxquant/front、tushare/qfq 逐一 `resolve_latest_ready`
- 缺失写入 `_missing_sources`
- `market_data` 不存在时视为两源皆缺
- `if _missing_sources: raise ValueError(... ready dataset ...)`

**测试**:

- `test_dual_source_fails_without_ready_datasets`：`pytest.raises(ValueError, match="ready dataset")` — **通过**
- `test_create_experiment_with_dual_source`：预置两源 ready 后创建，`assert None not in ds_ids` — **通过**
- 本机：`pytest -k dual_source` → **3 passed**

### 2.2 weekly_bar_mode 引擎侧 — **MOSTLY FIXED**

`backtest.py` 所有 **周/日聚合相关** `build_period_bars` 调用已带 `weekly_bar_mode=_weekly_bar_mode`（5 处）。  
`MONTH` 一处未传（合理，周线模式不作用月线）。

**残余 P2**：`study.build_period_bars` 在 `vendor_native` 时仅当 `vendor_weekly_bars is not None` 才用原生周线；`backtest.py` **未**加载/传入 `vendor_weekly_bars`。  
→ 选 `vendor_native` 时目前仍会 **回落 local_aggregate**，除非另有注入。参数已贯通，**真·原生周线数据路径未闭环**。

### 2.3 sync 层 symbol 标准化 — **FIXED**

`scripts/sync_market_data.py` `_normalize_symbol()`：

- `600000.SH` → `SSE.STK.600000`
- `000001.SZ` → `SZSE.STK.000001`
- `430047.BJ` → `BSE.STK.430047`
- 已是三段式则原样

独立探针断言全部通过。  
`store_bars` / `SymbolRecord` 使用 `norm_sym`；fetch 仍可用原始 vendor 代码（合理）。

### 2.4 双源 dataset 锁定 + 测试 — **FIXED**

见 2.1；成功路径 `dv["dataset_id"] = _resolved_ds[src.value]`（非 get 默认 None）。

---

## 3. 自动化测试

```
python -m pytest -q
→ 538 passed, 1 skipped, 4 warnings, ~83.17s
→ EXIT=0
```

与开发「538 passed, 0 failed, 1 skipped」**一致**（本环境亦 1 skipped）。

聚焦：

```
pytest -q tests/apps/astock/test_multi_source_integration.py -k dual_source
→ 3 passed
```

---

## 4. Gate（本轮）

| Gate | 状态 | 说明 |
|------|------|------|
| 1 | **PASS** | 同前 + sync 标准化 |
| 2 | **PARTIAL** | 实现完整度提升；**live 未跑** |
| 3 | **PASS** | weekly 参数已进 build_period_bars |
| 4 | **PASS** | 双源 hard-fail + 锁 id + 测试 |
| 5 | **PARTIAL** | 100 股/差异报告仍需真实数据 |

---

## 5. 残留问题

### P0

无

### P1（降级/环境类）

1. **Live** TdxQuant / Tushare 门禁未执行  
2. **100 股双源差异报告** 未执行  
3. （可选）完整 `BacktestService.run` + DB 六字段 E2E 仍可加强  

### P2

1. **`vendor_native` 未注入 `vendor_weekly_bars`**，模式选择可能 silent 回落聚合  
2. 根目录 CSV / `tmp` 探测文件不宜入库  
3. 工作区仍未 commit  

---

## 6. 合并与上线

| 建议 | |
|------|--|
| 功能分支 | **建议保留并继续 live 联调** |
| 主线「生产多源完成」 | **否** |
| 主线 CONDITIONAL | **可**，须写明 live + 规模验收未完成 |

`merge_recommended`: **conditional**  
`p0_count`: **0**  
`core_p1_claimed_closed`: **yes**（本轮声明的 4 项经核实关闭；vendor_native 数据注入为 P2）

---

## 附录

- 命令与日志：`tmp/multi_source_market_data_recheck3_commands.txt`、`tmp/_recheck3_pytest.txt`  
- JSON：`tmp/multi_source_market_data_recheck3_acceptance.json`
