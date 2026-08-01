# 多行情源改造 — 修复后独立复核验收

**复核日期**: 2026-07-25  
**角色**: 独立代码审查 / 验收测试（不修改正式代码、不 commit/push）  
**对照**: 上一轮 `docs/multi_source_market_data_independent_acceptance.md`（FAIL）及 `docs/multi_source_market_data_repair_report.md`（开发自述，仅参考）

---

## 1. 最终结论

### **FAIL — 修复未达正式完成标准**

相对上一轮 **FAIL**，本轮修复在 **端到端接线骨架** 上有实质进步：API / 实验 / `BacktestRequest` / Repository L1 分支 / 信号缓存 key / DB upsert 列 / `pytest.ini` 均已落地，默认全量测试 **533 passed, 1 skipped, 0 failed**。

但独立复测仍发现 **阻断级缺口**，不满足 PASS / CONDITIONAL PASS：

| 阻断点 | 说明 |
|--------|------|
| **P0-R1 证券代码格式不一致** | TdxQuant 同步/入库默认保留 `600000.SH` 形态；回测用 `SSE.STK.600000` 调 `load_bars` → **DatasetNotFoundError / 无法读 L1**（本轮脚本已复现） |
| **P0-R2 成功 run 仍不落库多源字段** | `upsert` 虽支持 6 列，但 `backtest_artifacts.append_run_index` 与实验 `_run_one` 成功路径 **未传入** `signal_data_source`/`dataset_id` 等 → 生产追溯仍断 |
| **Live / 真实双源回测** | 仍无独立 live 通过证据；集成测试名过满、多数未跑完整 `BacktestService.run` |

**不建议合并**用于生产多源回测。上一轮 4 个 P0 中，接线类 **大部分已关**，但 **持久化闭环与数据可用性** 仍 FAIL。

---

## 2. Git 状态（复核时）

| 项 | 值 |
|----|-----|
| 分支 | `feat/multi-source-market-data` |
| HEAD | `cad0742746aa6f675b6fc4b17798ee43b482c2df` |
| 工作区 | **不干净**（修复均在未提交 diff + 未跟踪文件） |
| 相对上一轮新增改动 | `api.py`, `backtest.py`, `experiments.py`, `db.py` 等；`pytest.ini`；`test_multi_source_integration.py` |

`git diff --stat`（已跟踪）约 **10 files, +381/-47**（另有未跟踪 providers/store/scripts/tests）。

---

## 3. 上一轮 P0 关闭情况（必须用代码/测试验证）

### P0-1 多源未接入回测/实验 — **大部分关闭，仍有数据可用性洞**

| 检查项 | 结果 | 证据 |
|--------|------|------|
| `BacktestBody` 含多源字段 | **是** | `api.py` ~74–79 |
| `api_backtest` 传入 `BacktestRequest` | **是** | `api.py` ~296–301 |
| `create_experiment_from_grid` 参数与 config | **是** | `experiments.py` ~858+、~1069+ |
| `_run_one` 构造 Request 含字段 | **是** | `experiments.py` ~1231–1244 |
| `backtest` resolve + lock `dataset_id` | **是** | `backtest.py` ~166–199 |
| L1 走 `MarketDataRepository.load_bars` | **是（tdxquant/tushare）** | `backtest.py` ~240–260 |
| `internal` 走 Repository | **否**（仍 legacy asof） | `_use_repository_l1` 仅 tdxquant/tushare |

**残留**: 见 P0-R1 符号格式；无 ready dataset 时正确 `ValueError`（好）。

### P0-2 生产信号缓存 key — **关闭**

`backtest.py` `_make_signal_cache_key` 现传入：

- `data_source`, `adjustment`, `dataset_id`, `weekly_bar_mode`, `anchor_date`, `execution_data_source`

执行缓存 payload（`backtest_context.py`）仍含 source/dataset 字段。  
静态审查通过；函数级隔离测试通过。

### P0-3 runs 新列 upsert — **半关闭**

| 层 | 状态 |
|----|------|
| `INSERT`/`UPDATE` 含 6 列 | **是**（`db.py` ~338+） |
| 单元测试直接 `upsert_run_from_index_row` 写入 | **通过** |
| **成功回测** `backtest_artifacts.append_run_index` | **未传** 多源字段（~399–430） |
| 实验成功后 `upsert_run_from_index_row` | **未传** 多源字段（~1252+） |
| `no_go` 路径 `append_run_index` | **未传** |

→ 开发称「P0-3 已关闭」**不完整**；库能力有了，**主路径仍写空**。

### P0-4 双源实验 — **产品路径已实现，运行时仍依赖 resolve**

| 检查项 | 结果 |
|--------|------|
| `dual_source_compare=True` 展开 2 源 | **是**（`experiments.py` ~1002–1018） |
| `dataset_id` 在双源 variant 上 | **强制 `None`** → 运行时 `resolve_latest_ready` |
| 真实 `create_experiment_from_grid` 测试 | **有**（`TestExperimentRealDualSourceExpansion`） |
| 端到端双源回测 | **未验证** |

---

## 4. 上一轮 P1 关闭情况（摘要）

| ID | 开发声称 | 独立结论 |
|----|----------|----------|
| P1-1 默认 pytest 收集 | 关闭 | **关闭**：`pytest.ini` `testpaths=tests` + `norecursedirs=tmp`；本轮 **533 passed** |
| P1-2 完整回测禁 Provider | 关闭 | **部分**：测试仍主要 patch + `repo.load_bars`，**未**完整 `BacktestService.run` |
| P1-3 301107 真实日期 | 部分 | **未关 live** |
| P1-4 TdxQuant 静默失败 | 关闭 | **基本关闭**：`_fetch_singles` 失败 raise `IncompleteResponse` |
| P1-5 adj_factor | 部分 | **API 有** `fetch_adj_factor`；**sync 增量仍未严格按因子变化重建** |
| P1-6 partial 加载 | 关闭 | **关闭**：默认 `allow_partial=False`；backtest 拒绝非 ready |
| P1-7 live markers | 关闭 | **标记已注册**；live 仍未跑通 |
| P1-8 weekly 贯通 | 关闭 | **字段贯通**；引擎 `build_period_bars` **未见**传入 `weekly_bar_mode=vendor_native` 实装 |

---

## 5. 本轮新发现问题

### P0-R1 证券代码格式不统一（阻断）

- **文件**: `providers/tdxquant.py`（symbol 原样写入）；`backtest.py` ~244 `symbol=code`（`SSE.STK.*`）；`tushare.py` 输出 `SSE.STK.*`
- **复现**（独立脚本）:
  1. dataset 中 symbol=`600000.SH`
  2. `repo.load_bars(dataset_id=..., symbol="SSE.STK.600000")`
  3. **实际**: `DatasetNotFoundError: Symbol SSE.STK.600000 not in dataset`
  4. **预期**: 能映射并加载，或同步阶段统一为 `SSE.STK.*`
- **影响**: 典型 TdxQuant CLI 同步后，**UI 选通达信前复权回测直接失败或无 L1 信号**；双源 A 腿不可用。
- **建议**: sync/normalize 统一 `to_std_code`；Repository 增加别名解析。

### P0-R2 成功任务多源字段仍不落库（阻断追溯/复现）

- **文件**: `backtest_artifacts.py` ~399–430；`experiments.py` ~1252–1280
- **实际**: append/upsert 行无 `signal_data_source`/`dataset_id`/…
- **预期**: 与锁定后的 `req.dataset_id` 一并写入
- **影响**: 结果页/DB 无法审计数据源；与「任务锁定 dataset」验收冲突
- **建议**: 所有 `append_run_index` / 实验 upsert 从 `req` 填 6 字段；`run_meta.repro` 同步写入

### P1-R3 `signal_adjustment` 未按源自动填充

- UI 可只传 `signal_data_source=tdxquant`
- `resolve_latest_ready(..., adjustment="")` 时 `list_datasets` **不按 adjustment 过滤**（空字符串为 falsy）
- 若同时存在 `none` 与 `front` ready，可能解析到错误口径
- **建议**: 使用 `SIGNAL_SOURCE_ADJUSTMENT` 默认映射；空 adjustment 禁止 resolve

### P1-R4 集成测试命名夸大

- `TestBacktestReadsRepositoryForTdxquant` 仅测 repo.load
- `TestFullBacktestNeverCallsProvider` 同
- **建议**: 真·`BacktestService.run` + tmp storage + fixture dataset（并统一 symbol）

### P1-R5 双源 variant 不在创建时锁定 dataset_id

- 两源均 `dataset_id=None`，跑时再 resolve latest → 并发同步时 **可复现性变差**
- **建议**: 创建实验时 resolve 并写入 params

### P2 其他

- 根目录 `SZSE.399*.csv`、`tmp/tdxquant_probe` 仍不宜入库  
- `internal` 高级源未接 Repository（可文档化为仍走本地 asof）  
- Live 门禁仍缺  

---

## 6. Gate 复核

| Gate | 上轮 | 本轮 | 说明 |
|------|------|------|------|
| 1 Provider/Repo/兼容 | PARTIAL | **PARTIAL→接近 PASS** | 层完整且 L1 已接线；符号/internal 残留 |
| 2 同步/301107/原子 | PARTIAL | **PARTIAL** | 逻辑在；live/符号未验 |
| 3 回测 source/锁/缓存 | FAIL | **PARTIAL** | 接线+缓存 key 好；锁/落库/符号差 |
| 4 实验 UI/双源 | FAIL | **PARTIAL** | 双源扩展真实现；结果追溯/UI 元数据仍弱 |
| 5 BSE/退市/100 股 | PARTIAL | **PARTIAL / NOT VERIFIED** | 单测有；真实与 100 股无 |

---

## 7. 自动化测试（本轮复测）

```
python -m pytest -q
→ 533 passed, 1 skipped, 4 warnings, ~66s
→ EXIT=0
```

- 默认路径 **不再** 因 `tmp/` 收集失败（`pytest.ini` 生效）。  
- **Live 未执行**（`live_tdxquant` / `live_tushare`）。  
- 不得将 533 通过等同生产可用。

---

## 8. 安全

- 未发现 Token 明文进入 git 跟踪内容。  
- `token_leak_found = false`

---

## 9. 问题计数（复核口径）

| 等级 | 数量 | 说明 |
|------|------|------|
| P0 | **2**（本轮仍开） | P0-R1 符号；P0-R2 成功路径落库 |
| P1 | **≥5** | adjustment 默认、测试夸大、创建时不锁 dataset、weekly 引擎、live |
| P2 | 若干 | 杂项文件、文档 |

上一轮 P0-1/2/4 接线面 **基本关闭**；P0-3 **仅半关闭**。

---

## 10. 是否建议合并

**否**（`merge_recommended = false`）

上线前至少关闭：

1. 全链路 symbol 标准化（sync + repository + 回测）  
2. 成功/失败 run 与实验 upsert **强制写入** 6 多源字段 + repro  
3. 创建时锁定 `dataset_id`（含双源）+ adjustment 默认映射  
4. 至少一条 **完整** `BacktestService.run` 集成测试（mock Provider 抛错 + fixture ready dataset）  
5. live 门禁或明确「仅单元通过、未 live」的发布标签  

---

## 11. 相对修复报告的纠偏

开发报告称多项「已关闭 / 533 全绿」——**全绿属实**；「P0 全关 / 生产可用」**不属实**。主要夸大点：

- 完整回测禁 Provider 测试名不副实  
- runs 持久化只测了 upsert API，未测主路径 append  
- 未披露 TdxQuant 与引擎 symbol 体系冲突  

---

## 附录 — 关键证据命令

见 `tmp/multi_source_market_data_recheck_commands.txt`。  
机器摘要见 `tmp/multi_source_market_data_recheck_acceptance.json`。
