# 历史股票宇宙说明

## 权威参考宇宙

```
来源: Tushare stock_basic (L/D/P) + local_vendor 并集
总条目: 5868
上市 (L): 5531
退市 (D): 338 (含 1 条 malformed 已排除)
暂停 (P): 0
```

每只证券记录 27 个字段：canonical_symbol, ts_code, exchange, board, instrument_type, list_status, list_date, delist_date, last_trade_date, name, vendor 覆盖状态, tushare 探针状态, adj_factor 可用性, tdxquant 可用性, 缺失原因, 纳入/排除状态, 审计标记等。

## 点时股票池 (Point-in-Time Universe)

```
universe_dataset_id: pit_universe_1d_20260717_bdd82bb209bd
universe_rule_version: pit_universe_rule_v1
instrument_identity_rule_version: identity_rule_v1
entries: 5868
content_sha256: bdd82bb209bd66c301a5cc234ab487c5600047af7416baaa321b37770358d2b7
```

### 成员资格规则

在日期 `d`，股票 `s` 是成员当且仅当：

```
list_date <= d
AND (delist_date IS NULL OR d <= last_trade_date)
```

### 排除原因

| 原因 | 说明 |
|------|------|
| `not_listed_yet` | 尚未上市 |
| `after_last_trade_date` | 已过最后交易日 |
| `delisted` | 已退市（无 last_trade_date 时 fail-closed） |
| `no_list_date_fail_closed` | 缺少上市日期，fail-closed 排除 |
| `unknown_symbol` | 不在宇宙中 |

### 纵向计数

| 年份 | 成员数 |
|------|--------|
| 2000 | 1059 |
| 2010 | 2039 |
| 2020 | 4179 |

### 北交所别名

242 个北交所迁移别名（如 `BSE.STK.839680` → `BSE.STK.920680`），通过 `aliases` 字段映射。

### 缓存隔离

- `universe_hash` 进入信号缓存键
- `universe_dataset_id` + `universe_rule_version` 进入 run_meta / SQLite / API
- 不同宇宙版本产生不同缓存键，不会混用
