# 多行情源改造 — 最终独立环境验收报告

**验收日期**: 2026-07-26  
**角色**: 独立环境验收（不修改生产代码、不 commit/push、不删除数据）  
**项目**: `E:\Software Development\wtpy-master`  
**分支**: `feat/multi-source-market-data`  
**HEAD**: `cad0742746aa6f675b6fc4b17798ee43b482c2df`

---

## 最终结论

# **CONDITIONAL PASS / BLOCKED**

| 维度 | 结果 |
|------|------|
| 默认 `python -m pytest -q` | **通过**（538 passed, 1 skipped, 0 failed） |
| 代码级 P0（接线/缓存/落库/双源 hard-fail） | **维持关闭**（与 recheck3 一致） |
| Live TdxQuant | **BLOCKED**（客户端未打开；health=False） |
| Live Tushare | **部分通过**（pytest 2/2 + 扩展探针；个别退市票无 daily） |
| 真实小规模 Tushare 同步 + Repository 读取 | **通过** |
| 真实 TdxQuant 同步 | **BLOCKED**（同客户端） |
| 产品双源对照（两源 ready） | **BLOCKED**（无 tdxquant ready dataset） |
| 真实 `BacktestService.run` + Provider mock=0 | **通过**（Tushare L1 + 锁定 dataset） |
| SQLite 六字段真实落库 | **通过**（成功 run 行非空） |
| 100 股双源报告 | **BLOCKED**（依赖双源 live 同步） |
| **merge_recommended** | **false** |

**不得判定正式 PASS**：正式 PASS 的硬门槛含 live_tdxquant、关闭通达信后双源离线复跑、产品双源两 variant、100 股报告等，本环境未全部满足。

---

## 1. 验收范围

按任务书执行：Git/工作区检查 → 默认 pytest → live markers → TdxQuant/Tushare live → 小规模同步 → missing/partial 拒绝 → 产品链路与 Provider 零调用 → L1/L2 价格样本 → 缓存 key → SQLite → 100 股（阻断记录）。

**未**修改生产代码；**未** commit/push；**未**删除原始通达信数据；**未**输出 Token。

---

## 2. 环境信息

| 项 | 值 |
|----|-----|
| OS | Windows |
| Python | 3.14.x |
| tqcenter 路径 | `D:\通达信\PYPlugins\user\tqcenter.py` **存在** |
| TdxQuant health | **False**（日志：请确认是否打开通达信客户端） |
| provider_version（离线） | `tdxquant_tqcenter_1.0.3` |
| Tushare | `1.4.29`，health **True**（`ts.get_token()`，无明文输出） |
| pytest.ini | `testpaths=tests`，`norecursedirs=tmp ...`，markers 已注册 |

---

## 3. Git 状态

```
branch: feat/multi-source-market-data
HEAD:   cad0742746aa6f675b6fc4b17798ee43b482c2df
```

- 工作区 **不干净**：11 个已修改跟踪文件 + 大量未跟踪（providers、dataset_store、repository、scripts、tests、docs、tmp、根目录 `SZSE.399*.csv`）。
- `git diff --check`：无 whitespace 错误输出。
- **无** 已跟踪删除（无 D）。
- 根目录临时 CSV：**存在**（`SZSE.399005_d.csv` 等）— 记录为不应入库，本轮不清理。
- `tmp/`：探测与验收产物存在；**pytest 默认不收集**（collect-only 无 tmp 路径）。

---

## 4. 默认测试验收

### 命令

```
python -m pytest -q
```

**不得**使用 `--ignore=tmp`（本轮未使用）。

### 结果

| 项 | 值 |
|----|-----|
| passed | **538** |
| failed | **0** |
| skipped | **1** |
| 耗时 | **~75.18s** |
| EXIT | **0** |
| 日志 | `tmp/_final_env_pytest.txt` |

### collect-only

```
python -m pytest -q --collect-only
→ 539 tests collected
→ tmp 探测脚本：未收集
```

Live 收集：

- `live_tdxquant`: 1 项（`test_live_301107_front_week`）
- `live_tushare`: 2 项（`test_live_daily_fetch`, `test_live_universe_delisted`）

### skipped 原因

```
SKIPPED tests/apps/astock/test_301107_tdxquant_regression.py:146
  TDX client not available
```

（默认套件中 live 测试因 health_check 失败被 skip，计入 1 skipped。）

---

## 5. Live TdxQuant — **BLOCKED**

### 命令

```
python -m pytest -q -m live_tdxquant -s
```

### 结果

| 项 | 值 |
|----|-----|
| 结果 | **1 skipped**（非 passed） |
| EXIT | 0（skip 不失败） |
| 日志 | `tmp/final_env_acceptance/live_tdxquant.txt` |
| 直接 health | `health False`；初始化错误：请确认是否打开通达信客户端 |
| 301107 周线 O/H/L/C=20.15/20.25/19.15/19.65 | **未验证** |
| 批量 / 000001 长历史 | **未验证** |
| 同步生成 tdxquant dataset | **失败** `client_unavailable` |

**判定**: **BLOCKED**（环境：通达信客户端未在线/未登录）。不得记 PASS。

---

## 6. Live Tushare — **PARTIAL PASS**

### 命令

```
python -m pytest -q -m live_tushare -s
→ 2 passed
```

扩展探针（无 Token 输出）：`tmp/final_env_acceptance/run_tushare_expanded.py`

| 检查 | 结果 |
|------|------|
| tushare 版本 | **1.4.29** OK |
| health | OK |
| daily（raw fetch） | OK n=22（600000） |
| pro_bar qfq | OK n=58（601088） |
| `fetch_adj_factor` | OK rows=22 |
| stock_basic L | OK n=5201 |
| stock_basic D | OK delisted=338 |
| BSE 宇宙 | OK n=334 sample `BSE.STK.920000` |
| BSE bars | OK n=360 |
| 301107 qfq | OK n=512 |
| 退市股 bars（样本 000003） | **FAIL** `DataNotDownloaded`（个例，非全失败） |

日志：`tmp/final_env_acceptance/tushare_expanded.json`（无 token 字段）。

**增量因子重建全历史**：sync 逻辑层此前代码审查为“不完整”；本轮 **未** 用真实因子变更事件做 live 重建证明 → 记 **NOT VERIFIED / 残留 P1**。

---

## 7. 真实小规模同步与 dataset 清单

### CLI

```
python scripts/sync_market_data.py --help
→ --symbol 支持逗号分隔（已确认）
```

### Tushare（成功）

```
--source tushare --mode full
--symbol 301107.SZ,601088.SH,000001.SZ
--start-date 20100101 --end-date 20240701 --anchor-date 20240701
--storage-root tmp/final_env_acceptance/market_data
```

| dataset_id | source | adj | status | success | rows |
|------------|--------|-----|--------|---------|------|
| `tushare_none_1d_anchor20240701_5abaecde9133` | tushare | none | **ready** | 3/3 | 7413 |
| `tushare_qfq_1d_anchor20240701_8d6188e93726` | tushare | qfq | **ready** | 3/3 | 7413 |

符号入库形态（标准化）：`SZSE.STK.301107`, `SSE.STK.601088`, `SZSE.STK.000001`。

### TdxQuant（失败）

```
status=failed, error=client_unavailable
```

### tdx_local（L2 样本）

第二次正确代码后：

| dataset_id | status | note |
|------------|--------|------|
| `tdxlocal_none_1d_20240701_eb0ebeb7b638` | **ready** | 3/3；符号为 **`sz301107`/`sh601088`/`sz000001`**（见新发现） |

首次错误代码 `sh301107` 曾产生 **partial**（记录在案，未当 ready 使用）。

### Repository 实测

- `000001.SZ` / `SZSE.STK.000001` 可读 tushare qfq  
- `SSE.STK.000001` 不可读（正确：该票为深市）  
- **独立 dataset_id**，未互相覆盖  

日志：`tmp/final_env_acceptance/sync_*.log`，清单 `tmp/final_env_acceptance/summary_bundle.json`。

---

## 8. missing / partial 拒绝

| 场景 | 结果 |
|------|------|
| 不存在 dataset_id | `DatasetNotFoundError` |
| partial manifest 默认 load | `DatasetNotReadyError` |
| resolve tdxquant/front 无数据 | `DatasetNotFoundError` |
| 双源实验无 tdxquant ready | **ValueError** 拒绝创建（产品路径） |

**未**观察到 silent fallback 到其他 source。

---

## 9. 双源产品链路

| 项 | 结果 |
|----|------|
| `create_experiment_from_grid(dual_source_compare=True)` 仅有 Tushare ready | **拒绝**：缺 tdxquant ready dataset |
| 两源真实 variant + 双 run | **未完成（BLOCKED）** |
| 测试手写 dict 冒充双源 | 本轮 **未** 用其充当产品通过证据 |

证据：`tmp/final_env_acceptance/product_path.json` / `.log`。

---

## 10. Provider 零调用 + 真实回测

### 命令/脚本

`tmp/final_env_acceptance/real_backtest.py`

- 入口：`BacktestService.run`（生产服务）
- L1：`signal_data_source=tushare`, `dataset_id=tushare_qfq_1d_anchor20240701_8d6188e93726`（锁定）
- monkeypatch：`TdxQuantProvider.fetch_bars` / `TushareProvider.fetch_bars` → 调用即 `AssertionError`
- 规则：`txt_先跌后涨新版5日外_研究去MIN60`，`research_unconfirmed_formula=True`
- 股票：`SZSE.STK.000001`，区间 20230101–20230331

### 结果

| 项 | 值 |
|----|-----|
| status | **ok** |
| run_id | `bt_1785024576_a7cf56` |
| Provider 调用次数 | **0** |
| 日志 | `tmp/final_env_acceptance/real_backtest.json` |

**关闭通达信后复跑**：客户端本已不可用，Tushare dataset 回测仍成功 → 满足「离线读 Repository」方向；**TdxQuant 源**无法对称验证。

---

## 11. L1 / L2 分离证据

### 数据集层对照（000001 同日）

| 日期 | L1 tushare qfq OHLC | L2 tdx_local raw OHLC | 相等 |
|------|---------------------|------------------------|------|
| 20240102 | 8.76/8.79/8.60/8.60 | 9.39/9.42/9.21/9.21 | 否 |
| 20240103 | 8.58/8.61/8.54/8.59 | 9.19/9.22/9.15/9.20 | 否 |
| 20240104 | 8.58/8.58/8.47/8.50 | 9.19/9.19/9.08/9.11 | 否 |

说明：前复权信号价与未复权本地价 **不同**，符合分离预期。

### 引擎路径说明（代码事实）

- L1 在 `signal_data_source in (tdxquant,tushare)` 时走 `MarketDataRepository.load_bars`  
- L2 `day_raw` 仍主要来自 **DataStore / TdxDayReader 本地文件**（非必须 `execution_dataset_id`）  
- 本轮成功 run 的 DB：`execution_data_source=tdx_local`，`execution_dataset_id=NULL`  

→ **架构意图 L2=raw 本地** 有运行与价格样本支持；**execution_dataset 锁定** 未在本轮产品 run 中启用。  
完整「逐笔成交价审计」未做深度交易流水拆解 → L1/L2 记 **PARTIAL 实证**。

---

## 12. 缓存隔离

生产 `signal_cache_key` 含 data_source / adjustment / dataset_id / weekly_bar_mode（代码 + 单元/集成测试历史通过）。

本轮静态确认 `backtest.py` 调用传入 `getattr(req, ...)` 字段。  
A/B/C/mode 不同 key 探针见历史 recheck；本轮未重复跑缓存击穿实验。

---

## 13. SQLite 落库

真实成功 run `bt_1785024576_a7cf56`：

```
signal_data_source   = tushare
signal_adjustment    = qfq
dataset_id           = tushare_qfq_1d_anchor20240701_8d6188e93726
weekly_bar_mode      = local_aggregate
execution_data_source= tdx_local
execution_dataset_id = NULL
status               = ok
```

→ **INSERT 路径真实写入** 非仅 schema。  
结果页 UI 本轮 **未** 启动浏览器人工验收 → 页面展示记 **NOT VERIFIED**。

---

## 14. 100 只股票双源差异报告 — **BLOCKED**

原因：

1. TdxQuant 客户端不可用，无法生成 tdxquant front 全样本；  
2. 双源对照依赖两源 ready；  
3. 本轮未消耗大规模 Tushare 额度做 100 股。

**不得**记 PASS。未生成 comparison CSV。

---

## 15. 4 个历史 P0 关闭证据（环境轮摘要）

| P0 | 状态 | 环境证据 |
|----|------|----------|
| 接线未接入 | 关闭 | 真实 `BacktestService.run` 使用 tushare dataset_id |
| 信号缓存 key | 关闭 | 生产调用传字段（代码） |
| runs 落库 | 关闭 | SQLite 实测六字段 |
| 双源假实现 | 关闭（产品 hard-fail） | 缺源拒绝创建；有源锁定测试在单元层 |

---

## 16. 新发现问题

### P1-ENV-1 TdxQuant live / 同步阻断

通达信客户端未打开 → 无法完成正式 PASS 所需 live 与双源。

### P1-ENV-2 tdx_local 同步符号未规范化为 `SSE.STK.*`

- `_normalize_symbol("sz000001")` 因含非标准两段式/前缀 **原样返回**  
- dataset 存 `sz000001`，`load_bars(symbol="SZSE.STK.000001")` **失败**  
- Repository 变体表 **未** 覆盖 `sh`/`sz` 前缀  
- **影响**：若未来 L2 强制走 Repository + std_code，将读失败；当前引擎 L2 仍走 TdxDayReader 可部分掩盖  

### P2 退市样本 000003 无 daily

扩展探针个例失败，不否定 stock_basic(D)，但说明退市行情覆盖需挑有效代码。

### P2 vendor_native

仍无 `vendor_weekly_bars` 注入闭环（recheck3 已记）。

### P2 工作区杂物

根目录指数 CSV、tmp 探测脚本 — 不应提交。

**本轮无新 P0（在「不修改代码」前提下记录 P1 符号缺口）。**

---

## 17. 正式 PASS 清单对照

| # | 要求 | 本轮 |
|---|------|------|
| 1 | 默认 pytest 直接通过 | **是** |
| 2 | 原 4 P0 关闭 | **是（代码+部分环境）** |
| 3 | 回测真实读 Repository | **是（Tushare L1）** |
| 4 | 创建/运行锁定 dataset_id | **是（req+DB）** |
| 5 | 回测 Provider=0 | **是（mock 证明）** |
| 6 | 关通达信后 dataset 可回测 | **Tushare 是；TdxQuant 无数据** |
| 7 | 双源缓存隔离 | **代码/单测是；双源实跑 BLOCKED** |
| 8 | SQLite 六字段 | **是** |
| 9 | 双源 experiments 产品路径 | **仅 hard-fail 侧；双成功 BLOCKED** |
| 10 | L1/L2 真实集成 | **部分（价差样本+路径）** |
| 11 | partial/missing 拒绝 | **是** |
| 12 | live_tdxquant | **否 BLOCKED** |
| 13 | live_tushare | **基本是（扩展 1 项弱）** |
| 14 | Token 无泄漏 | **是** |
| 15 | 无新 P0 | **是** |
| 16 | 无 silent fallback | **观察通过** |
| 17 | 双源单成功整成 | **N/A（未创建双源成功实验）** |
| 18 | 结果页追溯 | **NOT VERIFIED** |

---

## 18. 合并建议

**merge_recommended = false**

升级正式 PASS 前必须：

1. 打开通达信，跑通 `pytest -m live_tdxquant` 与 301107 目标周硬断言；  
2. 同步 tdxquant front + tushare qfq 最小样本；  
3. 产品路径创建双源实验，两 run 均成功，DB/结果可区分；  
4. Provider=0 + 关客户端复跑双源；  
5. 修复或验收 tdx_local `sh`/`sz` → std 规范化（若 L2 走 Repository）；  
6. （可选）100 股差异报告。

---

## 19. 交付物路径

1. `docs/multi_source_market_data_final_environment_acceptance.md`（本文件）  
2. `tmp/multi_source_market_data_final_environment_acceptance.json`  
3. `tmp/multi_source_market_data_final_environment_commands.txt`  
4. 证据目录：`tmp/final_env_acceptance/`  
   - `live_tdxquant.txt`, `live_tushare.txt`  
   - `tushare_expanded.json`, `sync_*.log`  
   - `real_backtest.json`, `product_path.json`  
   - `summary_bundle.json`  
5. 100 股报告：**未生成**
