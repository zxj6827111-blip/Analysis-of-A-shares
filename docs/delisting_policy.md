# 退市持仓处理说明

## 规则版本

```
delist_exit_rule_version: delist_exit_v1
```

## 三种情景

| 情景 | 说明 | 退出价格 |
|------|------|----------|
| `last_tradable_price` | 按最后可交易日收盘价退出 | 最后收盘价 |
| `discounted_recovery` | 折价回收 | 最后收盘价 × recovery_discount |
| `zero_recovery` | 零回收 | 0 |

### 参数

- `delist_recovery_discount`：折价系数，范围 [0, 1]，默认 0.5
- `delist_exit_apply_costs`：退出时是否收取手续费，默认 false

## 处理流程

1. 点时宇宙在 `last_trade_date` 之后将股票标记为 `after_last_trade_date`
2. 该股票不再产生新的买入信号
3. 如有持仓，在 `last_trade_date` 之后触发 `delist_terminal_exit`
4. 按选定情景计算退出价格
5. 交易记录标记 `exit_reason=delist_terminal_exit`
6. 退市损失单独统计为 `delist_realized_loss`

## 追溯字段

以下字段进入 run_meta / SQLite / API / export / 页面：

```
delist_exit_rule_version
delist_exit_scenario
delist_recovery_discount
delist_terminal_exit_count
delist_realized_loss
delisted_trade_count
delisted_open_positions_at_end
```

## 缓存隔离

退市规则版本和情景进入任务哈希和缓存键，不同规则产生不同结果，不会混用。

## Fail-Closed

- 未知情景 → `ValueError`
- discount 超出 [0,1] → `ValueError`
- 缺少 last_trade_date → 点时宇宙 fail-closed 排除
