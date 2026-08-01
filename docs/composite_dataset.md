# Composite 数据集说明

## 概述

Composite 数据集将 local_vendor（存量上市股）和 tushare（退市补充股）合并为统一的回测数据源，消除幸存者偏差。

## composite_none（L2 执行）

```
dataset_id: internal_composite_none_1d_20260717_3ea1824e3691
source: internal
adjustment: composite_none
symbols: 6107
rows: 17,174,691
```

### 父数据集

| 父集 | dataset_id | symbols | rows |
|------|-----------|---------|------|
| local_vendor | `localvendor_none_1d_20260726_7089dc09c3c0` | 5796 | 16,046,025 |
| tushare 退市补充 | `tushare_delisted_none_1d_20260717_f2572766019b` | 311 | 1,128,666 |

### 合并规则 (composite_merge_rule_version=1)

1. local_vendor 已有的股票 → 完整使用 local_vendor
2. local_vendor 完全缺失的股票 → 使用 tushare
3. **不允许** 同一只股票内部拼接两个来源
4. 两个父集的 symbol 交集为空（已验证）
5. 每只股票记录 `symbol_provenance`（来源父集 ID）

## composite_tushare_factor_qfq（L1 信号）

```
dataset_id: internal_composite_tushare_factor_qfq_1d_20260717_e0f994401233
source: internal
adjustment: composite_tushare_factor_qfq
symbols: 6107
rows: 16,815,207
formula_version: ctsfqfq_v1
```

### 父数据集

| 父集 | dataset_id |
|------|-----------|
| raw | `internal_composite_none_1d_20260717_3ea1824e3691` |
| factor (main) | `tushare_adjfactor_1d_20260726_acc8d3cadc79` |
| factor (supplement) | `tushare_adjfactor_1d_20260726_4957e64ddb81` |

### 因子来源分布

| 来源 | 数量 |
|------|------|
| main（精确匹配） | 5554 |
| supplement（退市股） | 311 |
| alias_main（北交所 920 别名） | 242 |
| 缺因子 | 0 |

## 不可变性

- 所有 composite 数据集发布后为 `status=ready`，不可修改
- 父数据集哈希记录在 manifest provenance 中
- 回测只锁定一个 `execution_dataset_id`，运行时不读取两个 L2 源
