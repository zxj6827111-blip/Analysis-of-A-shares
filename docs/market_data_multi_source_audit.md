# 多行情源与多前复权口径接入前架构审查报告

**审查时间**: 2026-07-25  
**分支**: `fix/standard-qfq-raw-execution` @ `cad0742`  
**测试基线**: 361 passed, 0 failed (43.25s)

---

## 1. 审查结论

| 维度 | 状态 | 说明 |
|------|------|------|
| 数据源抽象层 | **不存在** | TDX 硬编码贯穿全系统，无 Provider 接口 |
| TdxQuant 正式接入 | **未接入** | 仅有 `tmp/tdxquant_probe/` 探测脚本 |
| Tushare 正式接入 | **未接入** | 仅存在于通用 `datahelper` 层，与 astock 完全断开 |
| 仿射模型 | **已接入** | 4 条调用路径均已集成，affine-first + multiplicative fallback |
| 实验任务数据源字段 | **不存在** | 无 `signal_data_source` / `dataset_snapshot` / `weekly_bar_mode` |
| 周线生成 | **仅本地聚合** | ISO 8601 周，无直接读取通达信周线 |
| 退市股票 | **不支持** | 宇宙仅含当前上市股票，存在幸存者偏差 |
| 数据版本追踪 | **部分** | 有 SHA256 manifest，但无数据源标识 |

**总体评估**: 系统当前为单数据源（TDX 本地文件 + Baostock 复权因子）架构。要接入 TdxQuant/Tushare 双源，需要新建 Provider 抽象层、修改实验参数模型、增加数据源选择 UI。核心信号/执行分离架构（L1/L2/L3）设计良好，可复用。

---

## 2. 当前行情架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                        数据源层 (Source)                         │
├─────────────────────────────────────────────────────────────────┤
│  D:\通达信\vipdoc\{sh|sz}\lday\*.day  (32-byte binary, RAW)     │
│  Baostock API (foreAdjustFactor / dividend_data)                │
│  [无 TdxQuant 正式接入]                                         │
│  [无 Tushare 正式接入]                                          │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                     存储层 (Storage)                             │
├─────────────────────────────────────────────────────────────────┤
│  storage/astock/csv/day/{SSE|SZSE}/{code}.csv  (RAW, 5217只)    │
│  storage/astock/npz/day/{SSE|SZSE}/{code}.npz  (RAW, 5217只)    │
│  storage/astock/adjustments/{code}.json        (乘法因子)        │
│  storage/astock/adjustments/affine_{code}.json (仿射参数, 111只) │
│  storage/astock/calendar.json                  (交易日历)        │
│  storage/astock/universe.json                  (股票宇宙)        │
│  storage/astock/manifest.json                  (导入清单+SHA256) │
│  storage/astock/cache/signals/*.json           (信号缓存)        │
│  storage/astock/cache/execution/*.json         (执行缓存)        │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                     处理层 (Processing)                          │
├─────────────────────────────────────────────────────────────────┤
│  L1 信号价格: asof_forward_qfq (仿射优先, 乘法fallback)          │
│  L2 执行价格: raw (始终)                                        │
│  L3 公司行为: fail_closed (正式默认)                             │
│  周线/月线: 本地聚合 (ISO 8601 周)                               │
│  卦象OHLC: L2 raw 周线                                         │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                     消费层 (Consumers)                           │
├─────────────────────────────────────────────────────────────────┤
│  技术指标计算 (L1)                                              │
│  回测引擎执行 (L2)                                              │
│  卦象分类 (L2 raw 周线)                                         │
│  页面K线展示 (standard_qfq)                                     │
│  实验中心 (无数据源选择)                                         │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. 当前数据目录和数据库结构

### 3.1 行情数据文件

| 路径 | 格式 | 粒度 | 内容 | 数量 | 大小 |
|------|------|------|------|------|------|
| `storage/astock/csv/day/{SSE\|SZSE}/{code}.csv` | CSV | 每股 | RAW OHLCV | 5217 | 530.4 MB |
| `storage/astock/npz/day/{SSE\|SZSE}/{code}.npz` | NPZ | 每股 | RAW OHLCV | 5217 | 194.6 MB |
| `storage/astock/his/day/{SSE\|SZSE}/` | DSB | 每股 | RAW (空) | 0 | 0 |
| `storage/astock/parquet/` | Parquet | — | (空) | 0 | 0 |
| `D:\通达信\vipdoc\{sh\|sz}\lday\*.day` | Binary | 每股 | RAW 源文件 | ~5217 | — |

### 3.2 复权数据

| 路径 | 格式 | 粒度 | 内容 | 数量 |
|------|------|------|------|------|
| `storage/astock/adjustments/{EXCH}_STK_{code}.json` | JSON | 每股 | Baostock 乘法因子 | 5328 |
| `storage/astock/adjustments/affine_{EXCH}_STK_{code}.json` | JSON | 每股 | 仿射参数 (a,b) | 111 |
| `storage/astock/adjustments/复权因子_前复权.zip` | ZIP | 全市场 | 每日因子 (未使用) | 1 (150MB) |
| `storage/astock/adjustments/复权因子_后复权.zip` | ZIP | 全市场 | 每日因子 (未使用) | 1 (153MB) |

### 3.3 元数据

| 路径 | 内容 |
|------|------|
| `storage/astock/calendar.json` | 2559 交易日 (2016-01-04 ~ 2026-07-17) |
| `storage/astock/universe.json` | 5217 只 A 股 (schema_version=2, 含 survivor_bias_warning) |
| `storage/astock/manifest.json` | 5217 条导入记录 (source_sha256, first/last_date) |
| `storage/astock/config.json` | AStockConfig 序列化 |

### 3.4 缓存

| 路径 | 内容 | 失效机制 |
|------|------|----------|
| `cache/signals/{md5}.json` | 信号缓存 (含 factor_manifest_sha) | SHA 变化时失效 |
| `cache/execution/{md5}.json` | 执行结果缓存 | 参数 hash 变化时失效 |
| `cache/filtered_signals/{md5}.json` | 过滤后信号 | 同上 |

### 3.5 数据库

| 路径 | 类型 | 用途 |
|------|------|------|
| `{output_root}/astock_experiments.sqlite3` | SQLite WAL | 实验/任务/指标持久化 |
| `storage/astock/research_platform.db` | SQLite (36KB) | 研究平台试验记录 |

### 3.6 关键缺失

- **无数据源标识字段**: manifest 记录 `source_path` (TDX路径) 但无 `data_source` 枚举
- **无复权锚定日期**: 因子文件无 `anchor_date` 字段
- **无数据截止日期集中管理**: 分散在各 manifest 记录中
- **无内容哈希用于复权数据版本**: 因子文件有 `sha256`，但无全局快照版本

---

## 4. 当前价格口径

### 4.1 所有价格模式

| 模式 | 参数名 | 可选值 | 算法 | 用于信号 | 用于成交 | 用于页面 | 写入任务 |
|------|--------|--------|------|:---:|:---:|:---:|:---:|
| asof_forward_qfq | `signal_adjust` | 默认 | `factor_t/factor_asof` 或仿射 asof | ✓ | ✗ | ✗ | ✓ (run_meta) |
| standard_qfq | `signal_adjust` / `adjust` | 可选 | `factor_t/factor_end` 或仿射 anchor=last | 审计参考 | ✗ | ✓ | ✓ (display) |
| raw | `signal_adjust` / `adjust` | 可选 | 无调整 | 仅 research_unadjusted | ✓ | ✗ | ✓ |
| point_in_time | 内部 | — | `factor_t/base_factor` | ✗ | ✗ | 审计列 | ✗ |

### 4.2 复权模型选择

| 条件 | 使用模型 | 转换公式 |
|------|----------|----------|
| affine.quality=="complete" 且非 identity | 仿射 | `adj = a*raw + b` |
| 仿射不可用 | 乘法 | `adj = raw * (factor_t / factor_anchor)` |

### 4.3 关键回答

- **实验任务能否知道使用哪个行情源?** — **不能**。无 `data_source` 字段。仅记录 `factor_manifest_sha`。
- **回测结果能否知道使用哪版数据?** — **部分**。有 `factor_manifest_sha` 和 `engine_result_version`，但无数据源名称。
- **是否存在默认值覆盖用户选择?** — 是。`signal_adjust` 默认为 `asof_forward_qfq`，用户无法在实验 UI 中选择。
- **信号价格和成交价格是否使用同一套?** — **否**。严格分离：L1=asof_forward_qfq (信号)，L2=raw (执行)。

---

## 5. 当前回测信号和成交价格来源

### 信号价格 (L1)

```
DataStore.load_symbol(code) → day_raw [CSV/NPZ/TDX]
  → build_affine_series() [affine_{code}.json / Baostock API]
  → day_bars_for_signals_affine(signal_adjust="asof_forward_qfq", asof_date=end)
  → _affine_day_bars(bars, cum_a, cum_b)
  → build_period_bars(period) → 指标计算
```

### 执行价格 (L2)

```
DataStore.load_symbol(code) → day_raw [同一份 RAW]
  → PortfolioBacktester(bars_by_code=RAW)
  → bar_session_price(bar, session) * (1 ± slippage)
  → 涨跌停: prev_close from RAW
  → 估值: bar.close (RAW)
```

### 卦象 OHLC

```
day_raw → build_period_bars("WEEK") → aggregate_week()
  → BaguaCalculator.calculate(open, high, low, close) [L2 RAW 周线]
```

---

## 6. TdxQuant 接入现状

| 项目 | 状态 |
|------|------|
| 探测代码路径 | `tmp/tdxquant_probe/tdxquant_probe.py` |
| 正式 Provider | **不存在** |
| 标准 Bar 转换 | **不存在** |
| 缓存 | **不存在** |
| 批量同步程序 | **不存在** |
| 客户端在线检查 | **不存在** (探测脚本中隐含) |
| 登录状态检查 | **不存在** |
| 失败重试 | **不存在** |
| 调用限速 | **不存在** |
| 数据完整性检查 | **不存在** |
| 单股重建 | **不存在** |
| 全市场同步 | **不存在** |
| 增量同步 | **不存在** |
| 退市股票 | 未测试 |
| 全部历史数据 | 未测试 (需客户端已下载盘后数据) |
| 版本/快照保存 | **不存在** |

**已验证能力**: `dividend_type="front"`, `period="1d"/"1w"`, 代码格式 `301107.SZ`, 与客户端 100% 一致。

---

## 7. Tushare 接入现状

| 项目 | 状态 |
|------|------|
| Token 读取代码 | `wtpy/apps/datahelper/DHTushare.py` (通用层，非 astock) |
| `.tushare_token` 文件 | **未找到** |
| 正式 Provider (astock) | **不存在** |
| pro_bar 调用 | 仅在通用 datahelper 中 |
| 保存未复权 daily | **否** (astock 不使用) |
| 保存 adj_factor | **否** |
| 保存 qfq 日线 | **否** |
| anchor_date | **否** |
| 全市场同步 | **否** |
| 增量更新 | **否** |
| 频率限制处理 | **否** |
| 权限错误处理 | **否** |
| 网络失败处理 | **否** |
| 退市股票 | `stock_basic(list_status='L')` 仅上市 |
| 版本/快照 | **否** |

**关键发现**: Tushare 与 astock 模块完全断开。`wtpy/apps/astock/` 中无任何 tushare 引用。

---

## 8. 仿射/asof 复权现状

### 8.1 调用状态

| 函数 | 是否被调用 | 调用位置 |
|------|:---:|------|
| `compute_affine_params_asof()` | ✓ | `study.py:day_bars_for_signals_affine()` |
| `compute_affine_params()` | ✓ | `study.py:day_bars_for_signals_affine()` (standard_qfq) |
| `build_affine_series()` | ✓ | `bagua_query.py`, `backtest.py`, `cli.py`, `gua.py` |
| `fetch_baostock_dividend_events()` | ✓ | `build_affine_series()` 内部 (缓存未命中时) |

### 8.2 关键回答

| 问题 | 答案 |
|------|------|
| asof 是否真的被调用? | **是**，4 条路径均调用 |
| 是否支持 mode 和 asof_date? | 是，`signal_adjust` + `asof_date` 参数 |
| standard_qfq 和 asof_qfq 是否严格区分? | **是**，通过 `signal_adjust` 参数 |
| 是否可能使用未来公司行为? | **否**，asof 模式仅用 <= asof_date 的事件 |
| 数据是否只来自 Baostock? | **是** |
| 是否支持配股? | 代码支持 (rights_per_share, rights_price)，但 **Baostock 不返回配股数据** |
| 是否支持扩缩股? | 否 |
| JSON 缓存如何更新? | 首次构建后永久缓存，无自动刷新 |
| 新权益事件后是否自动重建? | **否** |
| 是否写入数据来源和质量状态? | 是 (`quality`, `source`, `sha256`) |
| 是否适合作为正式 asof_qfq 保留? | 是，但需补充配股数据源 |

---

## 9. 周线和月线生成现状

### 9.1 实现位置

| 函数 | 文件 | 用途 |
|------|------|------|
| `aggregate_week()` | `data/periods.py` | 唯一周线聚合实现 |
| `aggregate_month()` | `data/periods.py` | 唯一月线聚合实现 |
| `build_period_bars()` | `study.py` | 调度: DAY/WEEK/MONTH |
| `prepare_bars_for_bagua()` | `study.py` | 卦象用周线 (include_open=True) |
| `_week_key()` | `strategy_schedule.py` | 持仓周期跟踪 (重复实现) |

### 9.2 规则

| 项目 | 当前实现 |
|------|----------|
| 周线来源 | **仅本地日线聚合**，不直接读取通达信周线 |
| 周起始/结束 | ISO 8601 (周一~周日)，市场周为周一~周五 |
| 停牌周 | 仅聚合存在的日线，n_days 记录实际天数 |
| 节假日周 | 整周无交易则不生成 PeriodBar |
| 跨年周 | ISO 8601 处理 (isocalendar) |
| 日线精度 | 保留原始精度 (round(,4) 在仿射中) |
| 周线精度 | 聚合后不再额外 round |
| 成交量/额 | 求和 |
| 月线规则 | 按 (year, month) 分组，同 OHLCV 聚合 |
| 未闭合周/月 | 默认排除 (include_open=False) |
| 多实现? | 否，仅 `periods.py` 一处 |

---

## 10. 实验任务参数现状

### 10.1 当前创建实验表单字段

规则选择、止损%、止盈%、持仓天数、周期(DAY/WEEK/MONTH)、引擎(fast/full)、卦象过滤、卖出时段、买入模式、宇宙(demo/full)、日期范围、账户模式、实验名称、最大变体数、并发数。

### 10.2 缺失字段

| 字段 | 状态 |
|------|------|
| `signal_data_source` | **不存在** |
| `dataset_snapshot` | **不存在** |
| `weekly_bar_mode` | **不存在** |
| `adjustment_model` (affine/multiplicative) | **不存在** (隐式) |

### 10.3 数据库结构

- 表: `experiments` (config_json), `experiment_variants` (params_json), `runs`, `metrics`, `artifacts`
- 参数存储: JSON blob，无 schema 约束
- 旧任务兼容: 新增字段可用 `config_json` 默认值处理

### 10.4 建议改动位置

| 新增字段 | 改动文件 |
|----------|----------|
| `signal_data_source` | `service/experiments.py` (create), `service/db.py` (schema), `web/static/index_v3.html` (UI) |
| `dataset_snapshot` | `service/backtest.py` (run_meta), `data/catalog.py` (快照) |
| `weekly_bar_mode` | `study.py:build_period_bars()`, `data/periods.py` |

---

## 11. 本地与服务器数据同步现状

| 项目 | 状态 |
|------|------|
| 回测运行位置 | 本地 Windows |
| Linux 服务器部署 | 无 |
| 数据同步机制 | **不存在** (无 rsync/共享/对象存储/Git LFS) |
| 行情数据总容量 | CSV 530MB + NPZ 195MB + 因子 ~100MB ≈ **~825 MB** |
| 全市场十年日线估算 | ~5217 只 × 2559 天 × 32 bytes ≈ 430 MB (binary) |
| 服务端能否访问 D:\通达信 | **不能** (Windows 本地路径) |
| Tushare Token 服务端 | 需部署 `.tushare_token` 或环境变量 |

---

## 12. 股票主表与退市股票现状

| 项目 | 状态 |
|------|------|
| 股票列表来源 | TDX 本地 `.day` 文件扫描 |
| 仅当前上市? | **是** |
| 包含退市? | **否** |
| 保存上市日期? | 否 (仅 first_date 从数据推断) |
| 保存退市日期? | **否** |
| 证券更名/代码变更? | **否** |
| TdxQuant 退市股票? | 未测试 |
| Tushare stock_basic? | 仅在通用 datahelper，且 `list_status='L'` |
| 幸存者偏差? | **存在**，代码中有 3 处警告但无缓解措施 |

---

## 13. 测试基线

```
361 passed, 0 failed, 3 warnings (43.25s)
```

| 测试范围 | 文件 | 状态 |
|----------|------|------|
| 行情解析 | test_tdx_reader.py | ✓ |
| 复权 | test_adjustments.py, test_affine_adjust.py | ✓ |
| 周线聚合 | test_periods.py | ✓ |
| 回测 | test_backtest*.py | ✓ |
| 实验中心 | test_experiments.py | ✓ |
| 卦象 | test_bagua*.py | ✓ |
| 任务参数 | test_strategy_schedule.py | ✓ |
| API | test_api*.py | ✓ |
| 价格平面 | test_price_planes*.py | ✓ |

---

## 14. 可复用模块

| 模块 | 文件 | 复用方式 |
|------|------|----------|
| L1/L2/L3 价格平面 | `price_planes.py` | 直接复用，新增数据源不影响 |
| 周线/月线聚合 | `data/periods.py` | 直接复用 |
| 信号缓存 (SHA pinning) | `cache/signals/` | 需扩展 key 含 data_source |
| 仿射模型 | `data/affine_adjust.py` | 保留为 fallback / 交叉验证 |
| 回测引擎 | `strategy_engine.py` | 直接复用 (只消费 DayBar) |
| 实验网格扩展 | `service/experiments.py` | 需扩展参数轴 |
| DayBar 数据结构 | `data/` | 通用，任何 Provider 输出此格式即可 |
| 交易日历 | `data/calendar.py` | 直接复用 |
| 股票宇宙 | `data/universe.py` | 需扩展含退市 |

---

## 15. 必须重构/新建模块

| 模块 | 原因 | 优先级 |
|------|------|--------|
| **Provider 抽象层** | 当前无接口，TDX 硬编码 | P0 |
| **TdxQuant Provider** | 新建，封装 tqcenter 调用 | P0 |
| **Tushare Provider** | 新建，封装 pro_bar/pro.daily | P0 |
| **数据源选择参数** | 实验/回测需记录 source | P1 |
| **数据快照版本** | 可复现性 | P1 |
| **客户端在线检查** | TdxQuant 前置条件 | P1 |
| **频率限制/重试** | Tushare API 限制 | P1 |
| **退市股票支持** | 消除幸存者偏差 | P2 |
| **数据同步机制** | 服务器部署 | P2 |

---

## 16. 数据迁移风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| 仿射缓存仅 111 只 | 全市场回测仍用乘法 fallback | TdxQuant 接入后不再需要自行计算 |
| 因子文件无 anchor_date | 无法判断复权基准 | 新 Provider 需记录 |
| CSV/NPZ 无数据源标识 | 混入其他源数据无法区分 | manifest 增加 source 字段 |
| 信号缓存 key 不含 data_source | 切换源后可能命中旧缓存 | key 增加 source 维度 |
| zip 因子文件 (300MB) | 占用空间，未使用 | 可归档但本轮不删 |

---

## 17. 向后兼容风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| 旧实验无 `signal_data_source` | 查询/对比时缺失 | 默认值 `"tdx_local"` |
| 旧 run_meta 无数据源 | 历史结果无法追溯 | 从 `factor_manifest_sha` 推断 |
| 新增 Provider 参数 | 旧 API 调用不传 | 默认 `None` → 使用现有 TDX |
| 周线模式新增 | 旧任务无此字段 | 默认 `"local_aggregate"` |
| SQLite schema 变更 | 需 migration | 使用 `ALTER TABLE ADD COLUMN` |

---

## 18. 建议实施顺序

```
Phase 1: Provider 抽象层 (P0)
  ├── 定义 MarketDataProvider Protocol
  ├── 实现 TdxLocalProvider (包装现有 TdxDayReader + adjustments)
  ├── 实现 TdxQuantProvider (包装 tqcenter)
  └── 实现 TushareProvider (包装 pro_bar)

Phase 2: 数据源选择 (P1)
  ├── BacktestRequest 增加 signal_data_source 字段
  ├── 实验 config_json 增加 signal_data_source
  ├── 信号缓存 key 增加 source 维度
  └── 前端增加数据源选择下拉框

Phase 3: 数据完整性 (P1)
  ├── TdxQuant 客户端在线检查
  ├── Tushare 频率限制 + 重试
  ├── 数据快照版本 (dataset_snapshot_id)
  └── run_meta 记录完整数据源信息

Phase 4: 扩展 (P2)
  ├── 退市股票支持 (Tushare stock_basic list_status='D')
  ├── 全市场仿射缓存补全 (或用 TdxQuant 替代)
  ├── 服务器部署数据同步
  └── 周线模式可选 (本地聚合 vs 通达信直接)
```

---

## 19. 需要人工确认的问题

| # | 问题 | 影响 |
|---|------|------|
| 1 | TdxQuant 是否需要通达信客户端保持登录才能批量同步? | 决定是否能后台自动同步 |
| 2 | Tushare 积分是否足够全市场日线 + 复权因子? | 决定 Tushare 可用性 |
| 3 | 是否需要支持服务器部署? 还是仅本地 Windows? | 决定数据同步方案 |
| 4 | 旧实验结果是否需要标记数据源? 还是仅新实验? | 决定 migration 范围 |
| 5 | 仿射模型是否保留为第三数据源? 还是被 TdxQuant 替代? | 决定代码保留策略 |
| 6 | 退市股票优先级? 是否影响当前回测结论? | 决定 Phase 4 时间 |
| 7 | 通达信周线 vs 本地聚合周线是否允许用户选择? | 决定 weekly_bar_mode 设计 |
| 8 | 是否需要支持同一实验中对比不同数据源? | 决定实验网格设计 |

---

## 20. 精确修改文件候选清单

### Phase 1 (Provider 抽象)

| 文件 | 操作 | 说明 |
|------|------|------|
| `wtpy/apps/astock/data/provider.py` | **新建** | MarketDataProvider Protocol + 工厂 |
| `wtpy/apps/astock/data/tdx_local_provider.py` | **新建** | 包装 TdxDayReader + adjustments |
| `wtpy/apps/astock/data/tdxquant_provider.py` | **新建** | 包装 tqcenter |
| `wtpy/apps/astock/data/tushare_provider.py` | **新建** | 包装 pro_bar |
| `wtpy/apps/astock/data/__init__.py` | 修改 | 导出新 Provider |
| `wtpy/apps/astock/config.py` | 修改 | 增加 provider 配置 |

### Phase 2 (数据源选择)

| 文件 | 操作 | 说明 |
|------|------|------|
| `wtpy/apps/astock/service/backtest_request.py` | 修改 | 增加 signal_data_source |
| `wtpy/apps/astock/service/backtest.py` | 修改 | 根据 source 选择 Provider |
| `wtpy/apps/astock/service/experiments.py` | 修改 | 实验参数增加 source 轴 |
| `wtpy/apps/astock/service/db.py` | 修改 | schema migration |
| `wtpy/apps/astock/web/static/index_v3.html` | 修改 | UI 数据源选择 |
| `wtpy/apps/astock/price_planes.py` | 修改 | repro 字段增加 source |

### Phase 3 (完整性)

| 文件 | 操作 | 说明 |
|------|------|------|
| `wtpy/apps/astock/data/tdxquant_provider.py` | 修改 | 在线检查 + 重试 |
| `wtpy/apps/astock/data/tushare_provider.py` | 修改 | 频率限制 + 重试 |
| `wtpy/apps/astock/data/catalog.py` | 修改 | 数据快照版本 |
| `wtpy/apps/astock/service/backtest.py` | 修改 | run_meta 记录快照 |

### Phase 4 (扩展)

| 文件 | 操作 | 说明 |
|------|------|------|
| `wtpy/apps/astock/data/universe.py` | 修改 | 支持退市股票 |
| `wtpy/apps/astock/data/periods.py` | 修改 | 可选周线模式 |
| `wtpy/apps/astock/study.py` | 修改 | build_period_bars 支持直接周线 |
