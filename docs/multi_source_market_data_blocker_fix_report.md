# 多行情源改造 — 最终阻断问题修复报告

**日期**: 2026-07-26
**分支**: `feat/multi-source-market-data`
**HEAD**: `cad0742` (未 commit)
**结论**: **PASS** — 两个生产缺陷已修复，可进入 Live TdxQuant 验收

---

## 1. 问题一：TdxLocal Symbol 标准化

### 根因

`scripts/sync_market_data.py` 的 `_normalize_symbol()` 函数缺少对 `sh600000`/`sz000001`/`bj430047` 前缀格式的处理。当用户传入 `sz000001` 时，函数走到末尾 `return symbol` 原样返回，导致 dataset manifest 中存储了非标准符号。

同时 `MarketDataRepository._symbol_variants()` 也缺少 `sh`/`sz`/`bj` 前缀格式的变体生成，导致使用 `SZSE.STK.000001` 查询时无法匹配到存储为 `sz000001` 的记录。

此外 `TdxLocalProvider.fetch_bars()` 直接将传入的 symbol 传给 `TdxDayReader.read()`，而 reader 只理解 `sh600000`/`sz000001`/bare-6-digit 格式，不理解 `000001.SZ` 或 `SZSE.STK.000001`。

### 修复内容

| 文件 | 修改 |
|------|------|
| `scripts/sync_market_data.py` | `_normalize_symbol()` 新增 `sh`/`sz`/`bj` 前缀格式识别（8字符，前2为市场前缀，后6为数字代码） |
| `wtpy/apps/astock/data/repository.py` | `_symbol_variants()` 新增前缀格式变体生成：canonical→prefix, dot-suffix→prefix, prefix→canonical, bare→prefix |
| `wtpy/apps/astock/data/providers/tdx_local.py` | 新增 `_to_reader_code()` 静态方法，将任意格式转为 TdxDayReader 可识别的 `sh`/`sz`/`bj` 前缀格式；`fetch_bars()` 调用时先转换 |

### Canonical Symbol 规则

写入侧（sync）统一输出：`{EXCHANGE}.STK.{CODE}`
- 上海: `SSE.STK.600000`
- 深圳: `SZSE.STK.000001`
- 北交所: `BSE.STK.430047`

读取侧（Repository）兼容所有格式互相解析：
- `SSE.STK.600000` ↔ `600000.SH` ↔ `sh600000` ↔ `600000`
- `SZSE.STK.000001` ↔ `000001.SZ` ↔ `sz000001` ↔ `000001`
- `BSE.STK.430047` ↔ `430047.BJ` ↔ `bj430047` ↔ `430047`

### 旧 Dataset 兼容

旧 dataset（如 `tdxlocal_none_1d_20240701_eb0ebeb7b638`）中存储的 `sz301107`/`sh601088`/`sz000001` 格式，通过 Repository 读侧 `_symbol_variants` 扩展后可正确匹配。实测：

```
Old symbols: ['sz301107', 'sh601088', 'sz000001']
SZSE.STK.000001 from legacy dataset: 3449 bars  ✓
SSE.STK.601088 from legacy dataset: 3452 bars   ✓
```

无需删除或重建旧 dataset。

### 是否改变既定架构

否。仅扩展了符号解析的覆盖范围，未改变 Provider/Repository/DatasetStore 的接口或数据流。

---

## 2. 问题二：execution_dataset_id

### 设计核查结论

**情况一：现有设计要求锁定 execution dataset。**

证据：
1. `BacktestRequest` 定义了 `execution_dataset_id: Optional[str] = None` 字段
2. `runs` 表 schema 包含 `execution_dataset_id TEXT` 列
3. `db.py` 的 `upsert_run_from_index_row` INSERT/UPDATE 均包含该列
4. `backtest_artifacts.py` 的 `append_run_index` 传递 `execution_dataset_id`
5. `experiments.py` 的 `create_experiment_from_grid` 接受并传递 `execution_dataset_id`
6. 设计文档明确 L2 = tdx_local/none 固定执行价格源

**为什么之前为 NULL**：`backtest.py` 的 `run_backtest()` 只解析了 L1 signal dataset，从未解析 L2 execution dataset。字段存在但无赋值逻辑。

**为什么必须锁定**：本地 `.day` 文件会被通达信软件持续更新。如果不锁定 dataset_id，同一 run 在不同时间复跑可能得到不同 L2 价格，破坏可复现性。两个双源 variant 必须使用相同 L2 快照才能公平对比。

### 修复内容

| 文件 | 修改 |
|------|------|
| `wtpy/apps/astock/service/backtest.py` | 在 L1 dataset 解析后，新增 L2 execution dataset 解析逻辑：若 `execution_dataset_id` 已提供则验证 ready；否则 `resolve_latest_ready(source=tdx_local, adjustment=none)`；缺失则 hard-fail |
| `wtpy/apps/astock/service/backtest.py` | L2 数据加载路径：当 `_use_repository_l1` 且有 `_execution_dataset_id` 时，从 Repository 读取锁定 dataset 的 bars 作为 `day_raw`（执行价格）；否则走原有 DataStore/TdxDayReader 路径（legacy 兼容） |

### L2 是否使用锁定 raw dataset

是。当 `signal_data_source in (tdxquant, tushare)` 时：
- L1 信号价格：从 `signal_dataset_id` 对应的 Repository dataset 读取
- L2 执行价格：从 `execution_dataset_id` 对应的 Repository dataset 读取
- 买入、卖出、止盈止损、涨跌停、滑点、账户估值全部使用 `raw_map[code]`（即 L2 数据）

### Hard-fail 行为

缺少 ready 的 tdx_local/none dataset 时：
```
ValueError: No ready tdx_local/none execution dataset.
Run: python scripts/sync_market_data.py --source tdx_local --mode full
```

---

## 3. 真实集成测试结果

### 同步

```
python scripts/sync_market_data.py --source tdx_local --mode full \
  --symbol "000001.SZ,601088.SH" --start-date 20230101 --end-date 20240701 \
  --storage-root "storage/astock/market_data" --tdx-root "D:\通达信"

→ dataset_id: tdxlocal_none_1d_20240701_c7bb20ebe3eb
→ status: ready, 2/2 success, 720 rows
→ manifest symbols: ['SZSE.STK.000001', 'SSE.STK.601088']  (canonical)
```

### Repository 读取

```
SZSE.STK.000001: 360 bars ✓
SSE.STK.601088: 360 bars ✓
000001.SZ: 360 bars ✓ (dot-suffix variant)
601088: 360 bars ✓ (bare code variant)
```

### BacktestService.run

```
req:
  signal_data_source = tushare
  dataset_id = tushare_qfq_1d_anchor20240701_8d6188e93726
  execution_dataset_id = tdxlocal_none_1d_20240701_c7bb20ebe3eb

result:
  status = ok
  run_id = bt_1785026759_2e851f
  provider_calls = 0
```

### SQLite 六字段

```json
{
  "signal_data_source": "tushare",
  "signal_adjustment": "qfq",
  "dataset_id": "tushare_qfq_1d_anchor20240701_8d6188e93726",
  "weekly_bar_mode": "local_aggregate",
  "execution_data_source": "tdx_local",
  "execution_dataset_id": "tdxlocal_none_1d_20240701_c7bb20ebe3eb",
  "status": "ok"
}
```

ALL 6 FIELDS NON-NULL: **True**

---

## 4. 退市股票验证

### 000003.SZ 分析

| 项 | 值 |
|----|-----|
| ts_code | 000003.SZ |
| name | PT金田A |
| list_date | 19910703 |
| delist_date | 20020614 |
| 探针请求范围 | 2010-2024 |
| daily 条数 (2010-2024) | **0** |
| daily 条数 (1990-2001) | **2405** |
| 失败原因 | 退市日期 2002 年，探针使用 2010-2024 范围，超出交易期 |

**结论**：非 Provider 缺陷，是请求时间范围不覆盖交易期。

### 有效退市股票验证

| ts_code | name | list_date | delist_date | daily | adj_factor | status |
|---------|------|-----------|-------------|-------|------------|--------|
| 002898.SZ | 赛隆药业 | 20170912 | 20260717 | 2107 | 2145 | ok |
| 000004.SZ | 国华网安 | 19901201 | 20260714 | 3724 | 4012 | ok |
| 002808.SZ | 苏州恒久 | 20160812 | 20260714 | 2370 | 2406 | ok |

---

## 5. 默认测试结果

```
python -m pytest -q
→ 554 passed, 1 skipped, 0 failed (~112s)
```

新增 16 个 symbol normalization 测试（`test_symbol_normalization.py`）。

---

## 6. 新发现问题

无新 P0。

| 编号 | 严重度 | 描述 |
|------|--------|------|
| P2-1 | P2 | `vendor_native` 周线模式仍无 `vendor_weekly_bars` 注入闭环（已知，非本轮范围） |
| P2-2 | P2 | 根目录 `SZSE.399*.csv` 不宜入库（工程卫生） |

---

## 7. 是否可以进入 Live TdxQuant 验收

**是。** 所有代码级阻断已关闭：
1. 默认 pytest 直接通过 ✓
2. TdxLocal 新同步使用统一 canonical symbol ✓
3. 旧 sh/sz dataset 可以正确读取 ✓
4. Repository 可以读取上海、深圳样本 ✓
5. execution_dataset_id 设计已明确（必填，锁定） ✓
6. execution_dataset_id 已非空并实际锁定 ✓
7. L2 使用的数据与 execution_dataset_id 对应 ✓
8. SQLite 六字段全部非空 ✓
9. Provider 调用次数 = 0 ✓
10. 没有新增 P0 ✓

**下一步**：打开通达信客户端 → `pytest -m live_tdxquant` → tdxquant sync → 双源实验。

---

## 8. 修改文件清单

| 文件 | 类型 |
|------|------|
| `scripts/sync_market_data.py` | 修改（_normalize_symbol 扩展） |
| `wtpy/apps/astock/data/repository.py` | 修改（_symbol_variants 扩展） |
| `wtpy/apps/astock/data/providers/tdx_local.py` | 修改（_to_reader_code + fetch_bars） |
| `wtpy/apps/astock/service/backtest.py` | 修改（execution dataset 解析+锁定+L2 读取） |
| `tests/apps/astock/test_symbol_normalization.py` | 新增（16 个测试） |
