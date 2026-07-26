# A股回测系统多行情源与多前复权口径改造 — 项目总结

**日期**: 2026-07-25  
**分支**: `feat/multi-source-market-data`（基于 `fix/standard-qfq-raw-execution` @ `cad0742`）  
**状态**: CONDITIONAL PASS（代码级验收通过，环境验证待做）  
**测试**: 538 passed, 0 failed, 1 skipped（`python -m pytest -q` 直接通过）

---

## 一、改造目标

实验中心需要支持用户选择两套独立的前复权信号数据源：

| 信号源 | source | adjustment | 用途 |
|--------|--------|------------|------|
| 通达信前复权 | tdxquant | front | L1 信号价格 |
| Tushare前复权 | tushare | qfq | L1 信号价格 |
| 内部因果前复权 | internal | asof_qfq | L1 信号价格（高级） |
| 旧版兼容 | legacy_tdx_local_asof | asof_qfq | 旧任务默认 |

执行价格（L2）固定使用未复权真实价格：`tdx_local / none`。

核心约束：
- 信号与执行严格分离（L1 多源可选，L2 固定 raw）
- 不同来源独立存储、独立更新、不互相覆盖
- 任务创建后锁定 dataset_id，运行中不重新解析
- 回测运行中禁止调用外部接口（Provider）
- 禁止静默切换数据源
- 允许同一策略做通达信与 Tushare 双源对照实验

---

## 二、架构设计

```
┌─────────────────────────────────────────────────────────────────┐
│                    Provider 层（仅同步时使用）                     │
├─────────────────────────────────────────────────────────────────┤
│  TdxLocalProvider    → 包装 TdxDayReader，读 .day 文件           │
│  TdxQuantProvider    → 包装 tqcenter 1.0.3，batch=10，重试3次    │
│  TushareProvider     → 包装 tushare pro API，指数退避            │
│  InternalAsOfProvider → 包装 affine_adjust，因果 asof 语义       │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼ sync_market_data.py
┌─────────────────────────────────────────────────────────────────┐
│                    DatasetStore（内容寻址存储）                    │
├─────────────────────────────────────────────────────────────────┤
│  storage/astock/market_data/                                     │
│  ├── blobs/{sha256}.npz        ← 同一内容只存一次                │
│  ├── manifests/{dataset_id}.json ← 不可变 manifest              │
│  └── sync_logs/{sync_run_id}.json                                │
│                                                                  │
│  dataset_id = {source}_{adjustment}_{period}_{cutoff}_{sha[:12]} │
│  status: building → ready / partial / failed                     │
│  partial 不可用于回测                                            │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    MarketDataRepository（回测只读）               │
├─────────────────────────────────────────────────────────────────┤
│  resolve_latest_ready() → 只选 status=ready                      │
│  load_bars(dataset_id, symbol) → 符号别名解析（SSE.STK↔600000.SH）│
│  allow_partial=False（默认，回测不可读 partial）                  │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    回测引擎（BacktestService）                    │
├─────────────────────────────────────────────────────────────────┤
│  L1 信号: tdxquant/tushare → Repository.load_bars               │
│           internal → 现有 affine asof 路径                       │
│           legacy → 现有 affine/multiplicative 路径               │
│  L2 执行: 固定 tdx_local/none（DataStore.load_symbol）           │
│  缓存: signal_cache_key 含 data_source/dataset_id/weekly_mode    │
│  落库: runs 表 6 新列，所有写入路径均传递                         │
└─────────────────────────────────────────────────────────────────┘
```

---

## 三、已完成的改动清单

### 新增文件（核心代码）

| 文件 | 职责 |
|------|------|
| `wtpy/apps/astock/data/providers/__init__.py` | Provider 包导出 |
| `wtpy/apps/astock/data/providers/base.py` | 枚举、MarketBar、MarketDataRequest、异常族、Protocol |
| `wtpy/apps/astock/data/providers/tdx_local.py` | TdxLocalProvider |
| `wtpy/apps/astock/data/providers/tdxquant.py` | TdxQuantProvider（batch/重试/宽表归一化） |
| `wtpy/apps/astock/data/providers/tushare.py` | TushareProvider（指数退避/adj_factor/universe） |
| `wtpy/apps/astock/data/providers/internal_asof.py` | InternalAsOfProvider |
| `wtpy/apps/astock/data/dataset_store.py` | 内容寻址 blob + manifest + 原子发布 |
| `wtpy/apps/astock/data/repository.py` | MarketDataRepository（只读、符号别名、partial 限制） |
| `scripts/sync_market_data.py` | 同步程序（full/incremental/rebuild/audit） |
| `pytest.ini` | testpaths=tests, norecursedirs=tmp, live markers |

### 修改文件（生产接线）

| 文件 | 改动 |
|------|------|
| `api.py` | BacktestBody 新增 6 字段；实验创建传递 7 参数 |
| `service/backtest.py` | dataset resolve+lock；L1 走 Repository；缓存 key 传源字段；weekly_bar_mode 传入引擎 |
| `service/backtest_request.py` | 新增 6 字段 |
| `service/backtest_context.py` | 执行缓存 payload 含源字段 |
| `service/backtest_artifacts.py` | append_run_index 传 6 字段 |
| `service/experiments.py` | 签名+config+variant 注入+双源扩展+hard-fail+_run_one 传字段 |
| `service/db.py` | schema v2 迁移；upsert INSERT/UPDATE 6 列；_row_to_history 返回 |
| `research/signal_cache.py` | signal_cache_key 新增 7 参数 |
| `study.py` | build_period_bars 支持 weekly_bar_mode + vendor_native 报错 |
| `data/universe.py` | BSE/退市支持；SymbolInfo 新字段；save/load 兼容 |
| `web/static/index_v3.html` | 信号源下拉、周线模式、双源 checkbox |

### 新增测试（20 个文件，177 个测试）

涵盖：Provider 协议、归一化、无静默 fallback、manifest、内容寻址、原子发布、Repository 解析、缓存隔离、BacktestRequest 字段、dataset 锁定、旧任务兼容、双源扩展、周线模式、BSE、退市、301107 回归、符号格式解析、完整集成测试。

---

## 四、验收历程

| 轮次 | 结论 | 关键问题 |
|------|------|----------|
| 第一轮独立验收 | FAIL | 4 个 P0：UI 空壳、缓存未传、DB 不写、双源假测试 |
| 第一轮修复后复核 | FAIL | 2 个 P0：符号格式不一致、成功路径不落库 |
| 第二轮修复后复核 | CONDITIONAL PASS | P0=0；残留：双源 soft-fail、weekly 引擎、sync 符号 |
| 第三轮修复后复核 | CONDITIONAL PASS | 全部 P1 代码级关闭；仅剩环境验证 |

---

## 五、当前 Gate 状态

| Gate | 状态 | 含义 |
|------|------|------|
| Gate 1 Provider/Repo/兼容 | **PASS** | 抽象层完整，旧行为不变 |
| Gate 2 同步/301107/原子 | **PARTIAL** | 代码在，需 live 执行 |
| Gate 3 回测 source/锁/缓存 | **PASS** | 端到端接线+隔离+落库 |
| Gate 4 实验/双源/UI | **PASS** | 产品路径真实可用 |
| Gate 5 BSE/退市/100 股 | **PARTIAL** | 单测有，真实规模未做 |

---

## 六、尚未完成的事项（全部为环境/规模验证，非代码缺陷）

### 必须做（升级为正式 PASS 的条件）

1. **Live TdxQuant 验证**：通达信客户端在线时执行 `pytest -m live_tdxquant`，验证 301107 目标周 OHLC
2. **Live Tushare 验证**：有 token 时执行 `pytest -m live_tushare`，验证 daily/adj_factor/qfq/stock_basic
3. **小规模真实 sync**：3~5 只股票，分别 sync tdxquant + tushare，生成 ready dataset
4. **小规模双源回测**：用真实 dataset 跑 3 只股票双源实验，确认结果页 dataset_id 不同、缓存不串、L2 均 raw
5. **客户端离线复跑**：关闭通达信后，用已有 dataset 再跑回测，证明不依赖 Provider

### 建议做（提升质量）

6. 100 只股票双源差异报告（覆盖主板/创业板/科创板/北交所/退市/分红/送转/配股）
7. 一条强 E2E 测试：`BacktestService.run` → mock Provider 必炸 → 成功后读 SQLite 六字段
8. UI：无原生周线 dataset 时禁用 vendor_native 选项

### 工程卫生

9. 整理 commit 范围（核心代码+测试+pytest.ini 为一组；文档/tmp/csv 分开）
10. 根目录 `SZSE.399*.csv` 不宜入库
11. `tmp/tdxquant_probe` 为探测产物，不入库

---

## 七、关键设计决策记录

| 决策 | 理由 |
|------|------|
| 第一期继续 NPZ，不装 pyarrow/DuckDB | 无新依赖约束 |
| 内容寻址 blob 存储 | 同一内容只存一次，跨 dataset 共享 |
| dataset_id 含 manifest_sha[:12] | 不可变，内容变化即新 id |
| partial 不可回测 | 防止残缺数据污染结果 |
| 双源创建 hard-fail | 防止创建后运行才发现缺数据 |
| vendor_native 无 bars 时报错 | 不静默回落，用户明确知道不可用 |
| 符号别名在 Repository 读侧兼容 | sync 写侧已标准化为 SSE.STK.*，读侧兜底 |
| signal_adjustment 自动填充 | tdxquant→front, tushare→qfq，减少用户出错 |
| 旧任务默认 legacy_tdx_local_asof | 不猜测旧任务用了新源 |

---

## 八、文件统计

| 类别 | 数量 |
|------|------|
| 新增核心代码文件 | 10 |
| 修改生产文件 | 11 |
| 新增测试文件 | 20 |
| 新增测试用例 | ~177 |
| 总测试数 | 538 passed |
| 代码行增量（估） | ~3500 行新增，~200 行修改 |

---

## 九、给下一步执行者的建议

**如果目标是"升级为正式 PASS"：**

```bash
# 1. 通达信客户端在线时
python -m pytest -q -m live_tdxquant

# 2. 有 Tushare token 时
python -m pytest -q -m live_tushare

# 3. 小规模 sync（3只股票）
python scripts/sync_market_data.py --source tdxquant --mode full --symbol "301107.SZ,601088.SH,000001.SZ"
python scripts/sync_market_data.py --source tushare --mode full --symbol "301107.SZ,601088.SH,000001.SZ"

# 4. 通过 API 或 UI 创建双源实验（3只股票）
# 确认：两个 variant 用不同 dataset_id，结果页可追溯

# 5. 关闭通达信客户端，用已有 dataset 重跑
# 确认：回测成功，Provider 调用次数=0
```

**如果目标是"合入主线"：**

先整理 commit，建议分 2~3 个 commit：
1. `feat(astock): multi-source provider layer + dataset store + repository`
2. `feat(astock): wire multi-source into backtest/experiments/API/DB/cache`
3. `test(astock): multi-source integration + unit tests + pytest.ini`

文档和 tmp 产物不入库或单独 commit。

---

## 十、安全确认

- Token 未写入代码/配置/日志/报告
- 未修改原始通达信 .day 文件
- 未删除旧缓存/旧实验/旧仿射逻辑
- 未安装新依赖
- 未自动 commit/push
