# 幸存者偏差说明

## 问题定义

幸存者偏差（Survivorship Bias）是指回测时只使用当前仍然上市的股票名单来回看历史，导致：

1. 退市股票被排除，高估策略收益
2. 未来才上市的股票提前进入历史回测
3. 退市股票的亏损未被计入

## 治理措施 (Gate B)

| 阶段 | 措施 |
|------|------|
| B1 | 建立 2000-2026 权威历史参考宇宙（5868 只，含 338 只退市） |
| B2 | 从 Tushare 补充 311 只退市股的未复权日线 |
| B3 | 构建 composite 未复权执行数据集（6107 只） |
| B4 | 实现点时股票池，按历史日期动态纳入/排除 |
| B5 | 实现退市持仓强制退出（3 种情景） |
| B6 | 构建覆盖退市股的 composite QFQ 信号数据集 |
| B7 | 产品链路接线（baseline=survivorship_safe） |
| B8 | 独立验收 + 长周期 A/B 实验 |

## 状态标记

```
READY_FOR_MULTI_SOURCE_PRODUCTION_BACKTEST = true   (Gate C 后)
READY_FOR_SURVIVORSHIP_SAFE_BACKTEST = true          (Gate B8 后)
```

## 长周期实验验证

| 指标 | A (旧口径) | B (幸存者安全) |
|------|-----------|---------------|
| 股票宇宙 | 5554 | 6107 |
| 交易股票数 | 1616 | 1717 |
| 退市股交易 | 0 | 235 |
| 退市终止退出 | — | 3 |
| 退市实现损失 | — | -7.00 |
| 总收益差 (A-B) | +0.11% | — |

## 局限性

- TdxQuant/front 数据集不包含部分历史退市股票，因此 tdxquant 口径 **不是** 全历史幸存者安全的
- 幸存者安全口径仅适用于 `internal/composite_tushare_factor_qfq` + `internal/composite_none` + PIT universe 组合
