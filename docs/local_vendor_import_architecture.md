# 本地购买行情数据导入架构

**日期**: 2026-07-26
**状态**: 样本导入验证通过，待全量导入

---

## 1. 数据源概述

| 项 | 值 |
|----|-----|
| 来源 | 本地购买（第三方供应商） |
| 总大小 | 78.24 GB（292 个 ZIP） |
| 日K数据 | 2.02 GB（54 个 ZIP，2000-2026） |
| 分钟数据 | 72.35 GB（196 个 ZIP，暂不导入） |
| 复权因子 | 0.30 GB（2 个 ZIP） |
| 完全重复 | 33 个（SHA256 一致，浏览器重复下载） |
| 同名冲突 | 0 个 |

## 2. 日K数据格式

| 项 | 值 |
|----|-----|
| 容器 | ZIP（按年：2000.zip ... 2026.zip） |
| 内部结构 | `{YEAR}/{CODE}.{EXCHANGE}.csv` + `__MACOSX/` 元数据 |
| 编码 | utf-8-sig |
| 分隔符 | 逗号 |
| 股票代码 | `600000.SH` / `000001.SZ` / `688699.SH` |
| 日期格式 | `YYYY-MM-DD` |
| 复权状态 | **未复权（raw）** — 已交叉验证 |
| 成交量单位 | 手（100股） |
| 成交额单位 | 万元 |
| 价格精度 | 2位小数 |
| 组织方式 | 按股票（每只股票一个CSV文件） |

### 字段列表

```
code, datetime, open, high, low, close, pre_close, change, pct_chg,
volume, amount, turnover, turnover_free, volume_ratio, pe, pe_ttm,
pb, ps, ps_ttm, dv_yield, dv_ttm, total_share, float_share,
free_share, total_mv, circ_mv
```

### 未复权验证证据

000001.SZ 2024-01-02:
- 供应商数据: O=9.39 H=9.42 L=9.21 C=9.21
- tdx_local raw: O=9.39 H=9.42 L=9.21 C=9.21
- **完全一致** → 确认为未复权数据

## 3. 复权因子格式

| 项 | 前复权因子 | 后复权因子 |
|----|-----------|-----------|
| 文件 | 复权因子_前复权.zip | 复权因子_后复权.zip |
| 大小 | 150.3 MB | 153.2 MB |
| 股票数 | 5903 | 5903 |
| 编码 | utf-8-sig | utf-8-sig |
| 表头 | 股票代码,交易日期,复权因子 | 同 |
| 日期格式 | YYYYMMDD | YYYYMMDD |
| 前复权终止值 | 最新日=1.0 | 最早日=1.0 |
| 后复权终止值 | 最早日=1.0 | 最新日=165.33 |

**注意**: 这是供应商自有因子，不是 Tushare adj_factor，不得混用。

## 4. Provider 架构

```
LocalVendorProvider (仅同步时使用)
├── 扫描 incoming 目录，定位全日K子目录
├── 索引年度 ZIP (2000-2026)
├── fetch_bars(): 逐ZIP读取 → CSV解析 → 去重 → 标准化
├── 符号转换: 600000.SH → SSE.STK.600000
├── 交易所校验: 拒绝 SSE.STK.301107 等错误映射
└── 年度边界去重: 同一日期只保留一条记录
```

### source 标识

| 数据类型 | source | adjustment |
|----------|--------|------------|
| 日K原始价格 | `local_vendor` | `none` |
| 供应商前复权因子 | `local_vendor` | `vendor_front_factor` |
| 供应商后复权因子 | `local_vendor` | `vendor_back_factor` |

旧的 `tdx_local/none` 仅保留为历史兼容。

## 5. 导入流程

```
读取一个 ZIP
→ 解压到 E:\AStockData\temp\extract\<sync_run_id>\
→ 逐文件解析 CSV
→ 标准化字段 + 质量校验
→ 生成 NPZ blob
→ 记录 manifest + sync_log
→ 清理临时目录
→ 处理下一个 ZIP
```

### 断点续传

- 已处理 ZIP 通过 sync_log 中的 SHA256 跳过
- SHA256 变化后重新处理
- 失败记录在 errors 列表中
- partial dataset 不可用于回测

## 6. 数据质量规则

- trade_date 严格升序
- trade_date 不重复（年度边界自动去重）
- OHLC > 0
- high >= low
- low <= open <= high
- low <= close <= high
- 代码与交易所一致（SSE=5/6/9开头, SZSE=0/1/2/3开头, BSE=4/8开头）

## 7. 路径配置

```
LOCAL_VENDOR_RAW_ROOT = E:\AStockData\raw\local_vendor\original_files\incoming
LOCAL_VENDOR_EXTRACT_ROOT = E:\AStockData\raw\local_vendor\extracted
MARKET_DATA_ROOT = E:\AStockData\datasets\market_data
MARKET_DATA_TEMP_ROOT = E:\AStockData\temp
REPORT_ROOT = E:\AStockData\reports
```

正式回测只读 `MARKET_DATA_ROOT`，不读原始 ZIP。

## 8. 样本导入结果

| 项 | 值 |
|----|-----|
| dataset_id | `localvendor_none_1d_20240701_920cb49b0554` |
| status | **ready** |
| 股票数 | 4 |
| 总K线数 | 3543 |
| 日期范围 | 20200102 - 20240701 |
| Repository 读取 | 全部成功 |
| 交叉验证 | 000001.SZ 2024-01-02 close=9.21 ✓ |
