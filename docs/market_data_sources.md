# 行情数据源说明

## 数据源清单

| 数据源 | 用途 | 调整方式 | 离线可用 |
|--------|------|----------|----------|
| local_vendor | L2 执行价格 | 未复权 (none) | 是 |
| tushare | 复权因子 / 退市股补充日线 | adj_factor / none | 是（同步后） |
| tdxquant | L1 信号（通达信原生前复权） | front | 是（同步后） |
| internal | 派生数据集（composite） | composite_none / composite_tushare_factor_qfq | 是 |

## 正式数据集

### L2 执行

| dataset_id | source | adjustment | symbols | rows | status |
|------------|--------|-----------|---------|------|--------|
| `localvendor_none_1d_20260726_7089dc09c3c0` | local_vendor | none | 5796 | 16,046,025 | ready |
| `tushare_delisted_none_1d_20260717_f2572766019b` | tushare | none | 311 | 1,128,666 | ready |
| `internal_composite_none_1d_20260717_3ea1824e3691` | internal | composite_none | 6107 | 17,174,691 | ready |

### L1 信号

| dataset_id | source | adjustment | symbols | rows | status |
|------------|--------|-----------|---------|------|--------|
| `tdxquant_front_1d_20260726_09b179b48611` | tdxquant | front | 5547 | 16,651,526 | ready |
| `internal_tsfqfq_1d_20260717_c962acb8af26` | internal | tushare_factor_qfq | 5554 | 15,562,445 | ready |
| `internal_composite_tushare_factor_qfq_1d_20260717_e0f994401233` | internal | composite_tushare_factor_qfq | 6107 | 16,815,207 | ready |

### 复权因子

| dataset_id | source | symbols | rows | status |
|------------|--------|---------|------|--------|
| `tushare_adjfactor_1d_20260726_acc8d3cadc79` | tushare | 5796 | 16,545,201 | ready |
| `tushare_adjfactor_1d_20260726_4957e64ddb81` | tushare (supplement) | 311 | 1,195,150 | ready |

## 回测期间数据访问规则

- 回测运行时 **不调用** Tushare / TdxQuant / Baostock / 网络
- 所有数据从 content-addressed blob store 读取
- Provider 构造函数在回测期间不会被触发
- 每个 run 锁定唯一的 `dataset_id`（L1）和 `execution_dataset_id`（L2）

## 数据目录

```
E:\AStockData\datasets\market_data\   # 正式数据集（manifest + blobs）
E:\AStockData\raw\local_vendor\       # 原始供应商文件（仅同步时使用）
E:\AStockData\factors\tushare\        # Tushare 因子原始文件（仅同步时使用）
```
