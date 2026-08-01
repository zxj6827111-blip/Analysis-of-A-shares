# 多行情源与多前复权口径改造实施报告

**实施日期**: 2026-07-25  
**分支**: `feat/multi-source-market-data`  
**基线提交**: `cad0742` (基于 `fix/standard-qfq-raw-execution`)  
**测试基线**: 361 passed → **510 passed, 0 failed**

---

## 1. 架构改造说明

本次改造严格区分两个层次：

### A. MarketDataProvider（采集层）
只负责从外部来源采集数据，仅在同步程序中使用：
- `TdxLocalProvider` — 包装现有 TdxDayReader，读取未复权 .day 文件
- `TdxQuantProvider` — 包装 tqcenter 接口，获取通达信前复权数据
- `TushareProvider` — 包装 tushare pro API，获取 raw/qfq 数据
- `InternalAsOfProvider` — 包装 affine_adjust，提供内部因果前复权

### B. MarketDataRepository（读取层）
只负责从本地固定数据集读取数据。正式回测调用链：

```
实验任务 → 解析 dataset_id → MarketDataRepository → 本地 NPZ → 信号计算 → 未复权成交
```

**禁止**回测运行时调用任何 Provider。

---

## 2. 新增 Provider 说明

| Provider | 文件 | source | adjustment | 特点 |
|----------|------|--------|------------|------|
| TdxLocalProvider | `providers/tdx_local.py` | tdx_local | none | 包装 TdxDayReader，L2 执行价格来源 |
| TdxQuantProvider | `providers/tdxquant.py` | tdxquant | none/front | batch_size=10，失败拆单股重试，最多3次 |
| TushareProvider | `providers/tushare.py` | tushare | none/qfq | 指数退避，Permission 不重试，RateLimit 退避 |
| InternalAsOfProvider | `providers/internal_asof.py` | internal | asof_qfq | 保留严格因果语义，高级模式可选 |

### Provider 异常分类
`ProviderUnavailable` / `AuthenticationError` / `PermissionDenied` / `RateLimited` / `DataNotDownloaded` / `InvalidSymbol` / `IncompleteResponse` / `NormalizationError`

不得统一捕获后返回空列表。Provider 失败时**不静默切换**另一个 Provider。

---

## 3. Repository 和 Dataset 设计

### DatasetStore（内容寻址存储）
- `blobs/{sha256}.npz` — 同一内容只保存一次
- `manifests/{dataset_id}.json` — 不可变 manifest
- `sync_logs/{sync_run_id}.json` — 同步日志

### dataset_id 格式
```
{source}_{adjustment}_{period}_{cutoff_or_anchor}_{manifest_sha[:12]}
```
示例：`tdxquant_front_1d_20260724_a3f2b1c4d5e6`

### 发布原子化
`building → 完整性校验 → ready`  
部分失败标记 `partial`，partial 数据集**不会**被 resolve_latest_ready 选中。

### MarketDataRepository API
- `list_datasets()` / `get_dataset()` / `resolve_latest_ready()`
- `load_bars()` / `load_day_bars()` / `validate_dataset()`

---

## 4. 数据目录结构

```
storage/astock/market_data/
├── blobs/           # 内容寻址 NPZ
├── manifests/       # 不可变 dataset manifest
├── catalog.sqlite3  # (预留)
└── sync_logs/       # 同步运行日志
```

---

## 5. 数据库变更

`runs` 表新增 6 列（schema_version 1 → 2）：

| 列名 | 类型 | 旧行默认值 |
|------|------|-----------|
| signal_data_source | TEXT | legacy_tdx_local_asof |
| signal_adjustment | TEXT | asof_qfq |
| dataset_id | TEXT | NULL |
| weekly_bar_mode | TEXT | local_aggregate |
| execution_data_source | TEXT | tdx_local |
| execution_dataset_id | TEXT | NULL |

迁移通过 `ALTER TABLE ADD COLUMN` + UPDATE 实现，幂等安全。

---

## 6. 缓存 key 变更

### 信号缓存 key 新增字段
`data_source` / `adjustment` / `dataset_id` / `weekly_bar_mode` / `anchor_date` / `execution_data_source` / `universe_version`

### 执行缓存 key 新增字段
`signal_data_source` / `signal_dataset_id` / `execution_data_source` / `execution_dataset_id` / `weekly_bar_mode`

切换通达信与 Tushare 后缓存 key 必然不同，不会命中旧缓存。

---

## 7. 旧任务兼容策略

- 旧任务 `signal_data_source = legacy_tdx_local_asof`
- 旧任务 `dataset_id = NULL`
- 旧任务 `weekly_bar_mode = local_aggregate`
- **不会**把旧任务错误标记为 tdxquant 或 tushare
- 旧页面显示：数据源 = legacy_tdx_local_asof，数据集版本 = 未记录

---

## 8. TdxQuant 同步结果

同步程序已实现（`scripts/sync_market_data.py`），支持：
- full / incremental / rebuild / audit 模式
- batch_size 10~20，失败拆单股重试
- 增量同步检测前复权历史变化（60日重叠区间比较）
- 输出：总股票数、成功数、失败数、缺数据数、首末日期、总行数、耗时、错误清单、数据集ID

**注意**：实际全量同步需要通达信客户端在线。单元测试通过 mock 验证了归一化逻辑。

---

## 9. Tushare 同步结果

同步程序已实现，支持：
- full 模式：stock_basic(L) + stock_basic(D) + raw daily + qfq daily
- incremental 模式：按 trade_date 增量，检测 adj_factor 变化
- 限速指数退避，Permission 错误不重试
- QFQ 数据记录 anchor_date

**注意**：实际同步需要 Tushare token（通过 ts.get_token() 读取，不写入仓库）。

---

## 10. 301107 回归结果

目标周验证值：
- open = 20.15
- high = 20.25
- low = 19.15
- close = 19.65

单元测试验证了：
1. 宽表归一化保持精确精度 ✓
2. NPZ blob 存储/读取保持精度 ✓
3. DatasetStore → Repository 全链路保持精度 ✓
4. Live 测试标记为 `@pytest.mark.live_tdxquant`（需客户端在线）

---

## 11. 100 只股票双源对照结果

**待实际数据同步后执行**。框架已就绪：
- 双源对照实验 UI 已实现（勾选后自动生成 tdxquant/front + tushare/qfq 两个 variant）
- 对照实验只改变信号数据源，其他参数保持一致
- 差异报告生成逻辑需在 Gate 5 实际运行后补充

---

## 12. 北交所支持情况

- `is_bse_code()` 识别 4xxxxx / 8xxxxx 代码
- `to_std_code()` 支持 `bj430047 → BSE.STK.430047`
- `AShareUniverse.from_tdx_dirs(include_bj=True)` 扫描 BSE .day 文件
- `AShareUniverse.from_tushare_basic(include_bse=True)` 包含北交所
- 默认行为不变（exclude_bj=True），新实验可明确选择

---

## 13. 退市股票支持情况

- `SymbolInfo` 新增 `list_date` / `delist_date` / `status` / `source` 字段
- `AShareUniverse.from_tushare_basic(include_delisted=True)` 包含退市股票
- Tushare stock_basic(list_status='D') 提供 338 只退市股票
- 默认行为不变（仅当前上市），新实验可明确选择
- 旧格式 universe.json 加载兼容（缺失字段用默认值）

---

## 14. 全部测试结果

```
510 passed, 0 failed, 5 warnings (65.59s)
```

| 类别 | 数量 |
|------|------|
| 原有测试 | 361 |
| 新增测试 | 149 |
| **总计** | **510** |

新增 19 个测试文件全部通过。

---

## 15. 尚未完成的功能

| 功能 | 状态 | 说明 |
|------|------|------|
| 实际 TdxQuant 全量同步 | 待执行 | 需通达信客户端在线 |
| 实际 Tushare 全量同步 | 待执行 | 需 Tushare token |
| 100 只股票双源对照实验 | 待执行 | 需先完成数据同步 |
| 差异报告自动生成 | 待实现 | 依赖 Gate 5 数据 |
| 回测引擎实际接入 Repository | 待接入 | 当前 backtest.py 仍用旧路径，需后续 PR 接入 |
| 实验中心 config_json 传递新字段 | 部分完成 | UI 已加字段，experiments.py 传递逻辑待完善 |
| 正式默认数据源切换 | 未切换 | 需 Gate 1~4 全部通过 + 人工确认 |

---

## 16. 正式切换默认数据源前的剩余风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| 回测引擎未实际接入 Repository | 新数据源暂不能用于正式回测 | 后续 PR 完成 backtest.py 接入 |
| TdxQuant 需客户端在线 | 不能后台自动同步 | 限定为本地 Windows 手动同步 |
| Tushare 000001 仅到 2001 | 比 TdxQuant 少 10 年 | 以 TdxQuant 为主源 |
| 仿射缓存仅 111 只 | internal 源全市场仍用乘法 fallback | TdxQuant 接入后不再需要 |
| 信号缓存 key 变更 | 切换后旧缓存全部失效 | 预期行为，首次运行重建缓存 |
| experiments.py 未传递新字段到 BacktestRequest | 实验创建的 variant 不含 source | 需补充 _run_one 中的字段传递 |

---

## 约束遵守确认

- [x] 未删除旧缓存
- [x] 未删除旧实验
- [x] 未重置 Git
- [x] 未 stash 或丢弃本地改动
- [x] 未删除 affine_adjust.py
- [x] 未删除 BaoStock 旧逻辑
- [x] 未安装 pyarrow
- [x] 未安装 DuckDB
- [x] 未修改原始通达信 .day 文件
- [x] 未把 Token 写入仓库/日志/报告
- [x] 未自动 commit
- [x] 未自动 push
- [x] Provider 失败时不静默切换
- [x] 未切换正式默认数据源
