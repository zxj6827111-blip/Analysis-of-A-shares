# 多行情源架构审查 — 补充报告

**审查时间**: 2026-07-25  
**前置报告**: `docs/market_data_multi_source_audit.md`

---

## 一、TdxQuant 批量能力测试

### 1.1 返回数据结构

```python
# 返回 dict[str, DataFrame]
# key = 字段名 ("Open", "High", "Low", "Close", "Volume", "Amount")
# value = DataFrame, index=DatetimeIndex, columns=股票代码 (宽表)
{
    "Open": DataFrame(index=[dates], columns=["301107.SZ", "000001.SZ", ...]),
    "Close": DataFrame(...),
}
```

### 1.2 批量测试结果

| 测试 | 股票数 | 耗时 | 成功 | 记录数 | 备注 |
|------|--------|------|------|--------|------|
| 1只 1d none | 1 | 0.022s | ✓ | 134 | |
| 1只 1d front | 1 | 0.013s | ✓ | 134 | |
| 1只 1w front | 1 | 0.011s | ✓ | 28 | |
| 10只 1d none | 10 | 0.114s | ✓ | 134×10 | 单次调用 |
| 10只 1d front | 10 | 0.106s | ✓ | 134×10 | 单次调用 |
| 10只 1w front | 10 | 0.196s | ✓ | 28×10 | 单次调用 |
| 20只 1d none | 20 | 0.127s | ✓ | 宽表 | 单次调用 |
| 50只 1d none | 50 | 0.431s | ✓ | 宽表 | 单次调用 |
| 100只 (10批×10) | 90/100 | 0.895s | 90% | 1206 | 1批失败(代码不在缓存) |

### 1.3 关键发现

| 项目 | 结论 |
|------|------|
| 单次最大股票数 | **50只验证成功**，100只单次调用返回空 |
| 推荐批量大小 | **10~20只/次** |
| 是否需要每只重新初始化 | **否**，一次 `tq.initialize()` 即可 |
| 是否支持一次传多只 | **是**，stock_list 传列表 |
| 平均耗时 | **~10 ms/只** (含网络通信) |
| 全市场5217只估算 | 5217 × 10ms ≈ **52秒** (分批10只) |
| 客户端异常 | 未观察到崩溃，仅返回空数据 |
| 失败原因 | 个别代码不在客户端已下载盘后数据中 |

### 1.4 最大历史长度

| 股票 | 记录数 | 最早日期 | 耗时 |
|------|--------|----------|------|
| 000001.SZ | 8417 | **1991-04-03** | 0.41s |
| 000651.SZ | 6973 | 1996-11-18 | 0.30s |
| 600000.SH | 6353 | 1999-11-10 | 0.26s |
| 601088.SH | 4490 | 2007-10-09 | 0.19s |
| 301107.SZ | 1013 | 2022-05-24 | 0.05s |

**结论**: TdxQuant 可返回 IPO 至今全部历史，000001.SZ 覆盖 **35年**。远超 12 年需求。

### 1.5 本地数据对比

| 股票 | 本地CSV最早 | TdxQuant最早 | 差距 |
|------|------------|-------------|------|
| 000001.SZ | 2016-01-04 | 1991-04-03 | **25年** |
| 600000.SH | 2016-01-04 | 1999-11-10 | **16年** |
| 301107.SZ | 2022-05-24 | 2022-05-24 | 0 (IPO) |

本地数据仅从 2016 年开始，TdxQuant 可补全全部历史。

---

## 二、Tushare 权限与吞吐测试

### 2.1 Token 状态

| 位置 | 状态 |
|------|------|
| `~/.tushare_token` | 不存在 |
| 项目根目录 `.tushare_token` | 不存在 |
| 环境变量 `TUSHARE_TOKEN` | 不存在 |
| tushare 内部存储 (`ts.get_token()`) | **存在** |
| `DHTushare.py` 读取方式 | `ts.pro_api(**kwargs)` (使用内部存储) |

### 2.2 接口可用性

| 接口 | 可用 | 行数 | 耗时 |
|------|:---:|------|------|
| `stock_basic(list_status='L')` | ✓ | 5,531 | 0.30s |
| `stock_basic(list_status='D')` | ✓ | **338** | 0.11s |
| `stock_basic(list_status='P')` | ✓ | 0 | 0.10s |
| `daily` 单股10年 | ✓ | 2,564 | 0.20s |
| `daily` 全市场单日 | ✓ | 5,526 | 0.27s |
| `adj_factor` 单股10年 | ✓ | 2,564 | 0.10s |
| `adj_factor` 全市场单日 | ✓ | 5,544 | 0.16s |
| `pro_bar(adj='qfq')` 单股10年 | ✓ | 2,564 | 0.32s |
| 10只股票各1年 (循环) | ✓ | 233~243/只 | ~0.1s/只 |

### 2.3 关键数据

| 项目 | 值 |
|------|-----|
| 当前上市股票 | 5,531 |
| 退市股票 | **338** |
| 暂停上市 | 0 |
| 000001.SZ 最早日线 | **2001-03-07** (Tushare) vs 1991-04-03 (TdxQuant) |
| 全市场单日吞吐 | 5,526 行 / 0.27s |
| 单股10年吞吐 | 2,564 行 / 0.20s |
| 频率限制 | 未触发 (积分足够) |

### 2.4 全市场同步估算

| 方式 | 调用次数 | 估算耗时 |
|------|----------|----------|
| 按交易日 (daily trade_date) × 2559天 | 2,559 | ~12 min |
| 按股票 (daily ts_code) × 5531只 | 5,531 | ~10 min |
| adj_factor 按交易日 × 2559天 | 2,559 | ~7 min |
| **合计首次同步** | ~10,658 | **~30 min** |

---

## 三、历史覆盖范围

| 股票 | TDX本地 | TdxQuant none | TdxQuant front | Tushare daily | Tushare qfq |
|------|---------|---------------|----------------|---------------|-------------|
| 000001.SZ | 2016-01-04 | **1991-04-03** | 1991-04-03 | 2001-03-07 | 2001-03-07 |
| 000651.SZ | 2016-01-04 | 1996-11-18 | 1996-11-18 | ~1996 | ~1996 |
| 600000.SH | 2016-01-04 | 1999-11-10 | 1999-11-10 | ~1999 | ~1999 |
| 601088.SH | 2016-01-04 | 2007-10-09 | 2007-10-09 | ~2007 | ~2007 |
| 301107.SZ | 2022-05-24 | 2022-05-24 | 2022-05-24 | ~2022 | ~2022 |

**结论**: 
- TdxQuant 覆盖最长 (IPO 至今，000001 达 35 年)
- Tushare 覆盖次之 (000001 从 2001 年，约 25 年)
- 两者均满足 **12 年以上** 需求
- 本地 TDX 文件仅 2016 年起，需补全

---

## 四、股票宇宙范围

### 4.1 Tushare stock_basic 统计

| 状态 | 数量 |
|------|------|
| 当前上市 (L) | 5,531 |
| 退市 (D) | **338** |
| 暂停上市 (P) | 0 |
| **合计** | 5,869 |

### 4.2 本地 TDX 文件

| 项目 | 数量 |
|------|------|
| SSE .day 文件 | ~2,318 |
| SZSE .day 文件 | ~2,899 |
| BSE .day 文件 | **345** (存在但被排除) |
| 退市股票 .day 文件 | **存在** (600001/000003/600002 均有) |
| universe.json 包含退市 | **否** (仅 5217 只当前上市) |
| universe.json 包含 BSE | **否** (exclude_bj=true) |

### 4.3 关键发现

- TDX 本地**保留了退市股票文件**，但 universe 构建时排除了它们
- BSE 文件存在 (345只)，但被 `exclude_bj=true` 排除
- 退市股票 .day 文件最后修改于 2010 年 (数据完整到退市日)
- **无代码更名/曾用名历史**

---

## 五、存储格式基准

### 5.1 测试结果 (10只股票, 10年日线)

| 格式 | 总大小 | 单股读取 | 批量读取 | 精度 | 元数据支持 |
|------|--------|----------|----------|------|-----------|
| CSV | 1,342,650 B (1.3 MB) | 4.8 ms | 36.4 ms | ✓ | 需额外文件 |
| NPZ | 467,569 B (0.46 MB) | 1.2 ms | 10.0 ms | ✓ | 需额外文件 |
| Parquet | **未安装** (pyarrow/fastparquet) | — | — | — | 原生支持 |
| DuckDB | **未安装** | — | — | — | — |

### 5.2 推荐

| 场景 | 推荐格式 | 理由 |
|------|----------|------|
| 当前系统 (无新依赖) | **NPZ** | 最快 (3.5x CSV)，最小 (2.9x CSV)，已在使用 |
| 未来 (允许新依赖) | **Parquet** | 原生元数据、列裁剪、日期过滤、生态支持 |
| 全市场分析 | Parquet + DuckDB | 列式查询极快 |

### 5.3 全市场容量估算

| 格式 | 5217只 × 2559天 | 含35年历史 |
|------|-----------------|-----------|
| CSV | ~700 MB | ~1.5 GB |
| NPZ | ~240 MB | ~500 MB |
| Parquet (估) | ~150 MB | ~350 MB |

---

## 六、数据集版本设计核查

### 6.1 当前状态

| 组件 | 存在 | 字段 |
|------|:---:|------|
| `dataset_id` | **否** | — |
| `sync_run_id` | **否** | — |
| `factor_manifest_sha` | ✓ | 所有因子文件 SHA256 的聚合哈希 |
| `market_data_version` | ✓ | 信号缓存 key 中 (值未确认) |
| `calendar_version` | ✓ | 信号缓存 key 中 |
| manifest `source_sha256` | ✓ | 每只股票源文件哈希 |

### 6.2 信号缓存 key 完整字段

```python
signal_cache_key() 包含:
  schema, indicator_ids, indicator_source_hash, period,
  start, end, universe_hash, adjust_mode,
  factor_manifest_sha, market_data_version,
  calendar_version, combine, extra
```

**缺失**: `data_source` (tdxquant/tushare/baostock), `dataset_snapshot_id`

### 6.3 推荐 dataset_id 结构

```python
dataset_id = f"{source}_{sync_date}_{manifest_sha[:12]}"
# 例: "tdxquant_20260725_a3f2b1c4d5e6"
# 例: "tushare_20260725_7f8e9d0c1b2a"
```

### 6.4 推荐 sync_run_id 结构

```python
sync_run_id = f"{source}_{timestamp}_{n_stocks}_{status}"
# 例: "tdxquant_20260725T153000_5217_ok"
```

### 6.5 SQLite 新增列建议

| 表 | 新增列 | 类型 | 默认值 |
|---|---|---|---|
| `runs` | `signal_data_source` | TEXT | `'tdx_local'` |
| `runs` | `dataset_id` | TEXT | `NULL` |
| `experiments` | 无需新列 | — | 放 `config_json` |
| `experiment_variants` | 无需新列 | — | 放 `params_json` |

### 6.6 字段归属建议

| 字段 | 位置 | 理由 |
|------|------|------|
| `signal_data_source` | runs 独立列 | 高频查询/过滤 |
| `dataset_id` | runs 独立列 | 可复现性追溯 |
| `adjustment_model` | `config_json` | 低频，隐式 |
| `weekly_bar_mode` | `config_json` | 低频 |
| `sync_run_id` | 独立 sync_log 表 | 与回测解耦 |

### 6.7 旧任务兼容

- `signal_data_source` 默认 `'tdx_local'`
- `dataset_id` 默认 `NULL` (从 `factor_manifest_sha` 推断)
- 无需 migration 脚本，`ALTER TABLE ADD COLUMN` 即可

---

## 七、服务器计划确认

### 方案 A: 仅本地 Windows 运行

| 项目 | 状态 |
|------|------|
| 当前状态 | **已是此模式** |
| TdxQuant | 直接可用 (D:\通达信) |
| Tushare | 直接可用 (内部 token) |
| 数据同步 | 不需要 |
| 需修改模块 | 无 |

### 方案 B: Windows 采集 → Linux 服务器运行

| 需修改模块 | 说明 |
|------------|------|
| `data/data_store.py` | 增加 export/import 数据包功能 |
| `config.py` | 路径抽象 (去除 D:\ 硬编码) |
| `data/tdxquant_provider.py` | 仅 Windows 端运行，输出标准格式 |
| `data/tushare_provider.py` | 双端均可运行 |
| 新增 `sync/` 模块 | rsync/对象存储上传下载 |
| `service/backtest.py` | 数据路径从配置读取 |
| 部署脚本 | Docker/systemd 配置 |

| 数据量 | 估算 |
|--------|------|
| NPZ 全市场 | ~240 MB |
| 因子文件 | ~100 MB |
| 含35年历史 | ~500 MB |
| 传输方式 | rsync / S3 / 共享目录 |

---

## 八、最终结论

### 明确回答

| # | 问题 | 答案 |
|---|------|------|
| 1 | TdxQuant 是否适合全市场首次同步? | **是**。5217只 × 10ms ≈ 52秒。但需客户端在线且已下载盘后数据。 |
| 2 | Tushare 是否适合全市场首次同步? | **是**。按交易日查询 ~30分钟完成。无需客户端。 |
| 3 | 哪个源更适合做增量更新? | **Tushare**。无需客户端在线，按 trade_date 增量，0.27s/天。 |
| 4 | 能否覆盖至少12年? | **两者均可**。TdxQuant 最长35年，Tushare 最长25年。 |
| 5 | 是否支持北交所? | TDX 本地有 345 只 BSE 文件。Tushare stock_basic 含 BSE。当前系统排除 BSE。 |
| 6 | 是否支持退市股票? | TDX 本地**有**退市文件。Tushare 有 **338 只**退市数据。当前 universe 排除。 |
| 7 | 推荐存储格式? | 当前: **NPZ** (已用，最快)。未来: **Parquet** (需安装 pyarrow)。 |
| 8 | 推荐 dataset_id 设计? | `{source}_{sync_date}_{manifest_sha[:12]}` |
| 9 | 正式开发是否还存在未决阻塞项? | **是**，见下方。 |

### 未决阻塞项

| # | 阻塞项 | 影响 | 解决方案 |
|---|--------|------|----------|
| 1 | 无 Provider 抽象层 | 无法切换数据源 | 新建 `MarketDataProvider` Protocol |
| 2 | TdxQuant 需客户端在线 | 不能后台自动同步 | 限定为本地 Windows 使用 |
| 3 | 信号缓存 key 无 data_source | 切源后命中旧缓存 | key 增加 source 字段 |
| 4 | pyarrow 未安装 | 无法使用 Parquet | `pip install pyarrow` (需确认) |
| 5 | Tushare 000001 仅到 2001 | 比 TdxQuant 少 10 年 | 以 TdxQuant 为主源 |
| 6 | 退市股票未纳入 universe | 幸存者偏差 | 扩展 universe 构建逻辑 |

### 推荐数据源分工

| 用途 | 推荐源 | 理由 |
|------|--------|------|
| 前复权信号 (L1) | **TdxQuant** | 与通达信客户端 100% 一致 |
| 未复权执行 (L2) | **TDX 本地 .day** | 已有，最快 |
| 增量更新 | **Tushare** | 无需客户端，按日增量 |
| 退市股票补全 | **Tushare** | stock_basic(D) + daily |
| 复权因子交叉验证 | **Tushare adj_factor** | 独立验证仿射模型 |
| 全市场首次同步 | **TdxQuant** (52s) 或 **Tushare** (30min) | TdxQuant 更快但需客户端 |
