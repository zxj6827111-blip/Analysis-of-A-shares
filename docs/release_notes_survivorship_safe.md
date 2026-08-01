# 幸存者安全回测 发布说明

## 版本

```
分支: feat/multi-source-market-data
基线 HEAD: 8ee03196aa20a4ce10f8674ead55dd34b33867e9
Gate B 完成日期: 2026-07-27
```

## 新增能力

### 幸存者安全回测 (survivorship-safe backtest)

- 点时股票池：按历史日期动态纳入/排除股票
- 退市股覆盖：311 只退市股从 Tushare 补充
- 退市持仓处理：3 种退出情景（last_tradable_price / discounted_recovery / zero_recovery）
- composite 数据集：6107 只股票统一执行和信号数据
- 北交所别名：242 个迁移代码自动映射

### 使用方式

```python
# API 创建幸存者安全回测
POST /api/v1/backtests
{
    "rule_ids": ["tn6_735金叉及趋势"],
    "codes": [...],
    "start": 20000101,
    "end": 20260717,
    "baseline": "survivorship_safe"
}
```

baseline=survivorship_safe 自动解析：
- L1: `internal_composite_tushare_factor_qfq_1d_20260717_e0f994401233`
- L2: `internal_composite_none_1d_20260717_3ea1824e3691`
- Universe: `pit_universe_1d_20260717_bdd82bb209bd`
- Delist: `delist_exit_v1` / `last_tradable_price`

### 旧口径兼容

旧数据集和旧 run 完全不受影响：
- `internal_tsfqfq_1d_20260717_c962acb8af26` 继续可用
- `localvendor_none_1d_20260726_7089dc09c3c0` 继续可用
- `tdxquant_front_1d_20260726_09b179b48611` 继续可用
- 旧 run 可复现，结果不变

## 新增文件

### 生产代码
- `wtpy/apps/astock/data/composite_dataset.py`
- `wtpy/apps/astock/data/dataset_binding.py`
- `wtpy/apps/astock/data/historical_universe.py`
- `wtpy/apps/astock/data/pit_universe.py`
- `wtpy/apps/astock/data/tushare_delisted_sync.py`
- `wtpy/apps/astock/delist_policy.py`
- `wtpy/apps/astock/service/baseline.py`

### 测试
- `tests/apps/astock/test_b8_factor_resolution.py`
- `tests/apps/astock/test_composite_dataset.py`
- `tests/apps/astock/test_composite_qfq_derivation.py`
- `tests/apps/astock/test_delist_policy.py`
- `tests/apps/astock/test_factor_cache_isolation.py`
- `tests/apps/astock/test_factor_signal_backtest_path.py`
- `tests/apps/astock/test_historical_universe.py`
- `tests/apps/astock/test_pit_universe.py`
- `tests/apps/astock/test_survivorship_chain_wiring.py`
- `tests/apps/astock/test_tushare_delisted_sync.py`
- `tests/apps/astock/test_tushare_factor_dataset.py`
- `tests/apps/astock/test_tushare_factor_qfq_derivation.py`

### 同步脚本
- `scripts/sync_tushare_delisted.py`

### 文档
- `docs/market_data_sources.md`
- `docs/adjustment_semantics.md`
- `docs/composite_dataset.md`
- `docs/historical_universe.md`
- `docs/survivorship_bias.md`
- `docs/delisting_policy.md`
- `docs/dataset_operations.md`
- `docs/dataset_backup_restore.md`
- `docs/data_sync_resume.md`
- `docs/release_notes_survivorship_safe.md`
- `docs/final_acceptance_report.md`

## 测试结果

```
890 passed, 0 failed, 4 warnings
```

## 已知限制

1. TdxQuant/front 不包含部分历史退市股，不是全历史幸存者安全
2. SQLite 加列 DDL 无法完全事务回滚（使用幂等重试补偿）
3. 3 只退市股在 Tushare 也无数据（expected_missing）
