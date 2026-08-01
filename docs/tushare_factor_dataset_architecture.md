# Tushare 因子数据集与派生前复权架构(Gate C 第一阶段)

| 项目 | 说明 |
|---|---|
| 状态 | 已实现 |
| 日期 | 2026-07-26 |
| 分支 | `feat/multi-source-market-data`(基线 HEAD `8ee03196`,本轮改动未提交) |
| 关联 Gate | Gate C 第一阶段(READY_FOR_TUSHARE_FACTOR_SIGNAL) |

## 1. 三层固定架构

```
┌─────────────────────────────────────────────────────────────┐
│ L1 信号层(派生, 不可变)                                      │
│   source=internal  adjustment=tushare_factor_qfq  period=1d  │
│   = local_vendor raw OHLC × (adj_factor_asof / anchor_factor)│
│   volume/amount 原样继承 raw(不复权)                         │
├─────────────────────────────────────────────────────────────┤
│ 因子层(独立 FACTOR dataset, 不可变)                          │
│   source=tushare  adjustment=adj_factor  dataset_type=factor │
│   blob = npz{trade_date:int64, adj_factor:float64} 每股一 blob│
├─────────────────────────────────────────────────────────────┤
│ L2 执行层(既有, 不可变, 本轮不动)                            │
│   source=local_vendor  adjustment=none                       │
│   execution_dataset_id=localvendor_none_1d_20260726_7089dc09c3c0│
└─────────────────────────────────────────────────────────────┘
```

**命名红线**:local_vendor raw × Tushare factor 的派生结果标识为 `internal/tushare_factor_qfq`,**绝不**标记为 `tushare/qfq`。`tushare/qfq`(pro_bar 原生)仅作抽样校验,不做全市场主数据。

## 2. 因子数据集(source=tushare, adjustment=adj_factor)

- **不是 MarketBar**:`dataset_type="factor"`,blob 仅含 `trade_date` 与 `adj_factor` 两列;`DatasetStore.store_factors()` 强制校验日期严格升序、因子>0,违规拒存。
- 原始下载 CSV 按 `sync_run_id` 版本化保存于 `E:\AStockData\factors\tushare\adj_factor\<sync_run_id>\<ts_code>.csv`;发布后的不可变 dataset(blob+manifest)进入正式根 `E:\AStockData\datasets\market_data`。
- manifest 关键字段:dataset_type=factor、universe_file+universe_sha256(冻结宇宙)、content_hash、`token_exposed=false`、incremental_policy_version=`factor_inc_v1`、provenance(API/限速/调用统计/原始缓存目录)、expected/imported/no_data/failed 计数。
- 同步入口(第 25 节合规——与下载 daily 的旧 `--source tushare` 通道显式区分):

```bash
python scripts/sync_market_data.py --source tushare --adjustment adj_factor --mode full \
  --universe-file <冻结宇宙CSV> --rate-per-min 400 --coverage-out <映射表CSV>
```

特性:每分钟限速、Provider 内建指数退避重试、每 25 只原子写 checkpoint、`--resume` 沿用原 sync_run_id 断点续传、Windows 排他锁(作用域 `(root, tushare, adj_factor, 1d)`)、Token 仅经 `ts.get_token()` 读取且任何日志/manifest/异常输出均不含 Token。

- **ready 策略**:provider/quality 失败=0 才 ready;`no_factor`(Tushare 无该股因子,典型为最新年缺席的北交所股票)如实计数并在映射表中分类(`no_factor_expected`/`no_factor_unexpected`),不阻断因子集 ready——因为派生宇宙定义为 raw∩factor,缺因子股票在派生层被显式排除。

## 3. 符号映射

canonical `SSE.STK.600000` ↔ ts_code `600000.SH` 为纯规则互转(`_to_ts_code/_from_ts_code`,含 .BJ),无单票硬编码。映射表(coverage CSV)串联:canonical/vendor symbol/ts_code/exchange/board/instrument_type/list_status(stock_basic L+D)/list_date/delist_date/raw_available/factor_available/inclusion_status/exclusion_reason。

## 4. 派生前复权(source=internal, adjustment=tushare_factor_qfq)

- 公式版本 `formula_version=tsqfq_v1`;anchor 策略 `last_factor_on_or_before_cutoff`。
- `ratio(t) = adj_factor_asof(t) / anchor_factor`,asof=**该日或之前**最后一个有效因子(`searchsorted right - 1`),**从不使用未来因子回填历史**;raw 日期早于首个因子日的行整行丢弃并记 `leading_gap_rows_dropped`(可审计)。
- 边界处理:cutoff 非交易日→anchor 取 ≤cutoff 最后因子;早退市→anchor 为其最后因子;停牌日 raw 无 bar 自然跳过;无 anchor→该股 failed→整体 partial;因子≤0/日期乱序在因子层已拒存;重复因子日期在同步层去重(keep-last,计数);北交所与退市股同规则。
- 价格 `np.round`(银行家舍入)4 位小数存储,对比口径 2 位;`volume_policy=copied_from_raw_shares_no_adjustment`、`amount_policy=copied_from_raw_cny_no_adjustment`(**绝不**把量额乘价格比例);bar schema 无 pre_close 列,规则记录于 provenance(下游如需=前一 qfq close)。
- manifest 完整血缘:raw_dataset_id+raw_dataset_sha256(manifest 文件哈希)/factor_dataset_id+factor_dataset_sha256/anchor_policy/formula_version/price_precision_policy/volume_policy/amount_policy/universe_file+sha/content_hash,并继承 raw 的 survivorship_bias=true 全套警示字段。上一轮 local_vendor manifest 缺失的 universe_sha256/单位策略等,在本轮新 dataset 全部补齐——**不回写旧不可变 manifest**。
- 派生入口:

```bash
python scripts/sync_market_data.py --source internal --mode derive \
  --raw-dataset-id <local_vendor ready> --factor-dataset-id <factor ready> [--cutoff YYYYMMDD]
```

父集校验:raw 必须 local_vendor/none ready;factor 必须 dataset_type=factor 且 ready(partial 拒绝);全部成功才 ready,任何 failed/no_data → partial。

## 5. 增量更新策略(factor_inc_v1)

每日流程:下载最新 adj_factor → 与上一 factor dataset 逐股 blob 哈希比较 → **因子未变的股票只追加新交易日;因子变化(公司行为/历史修订)的股票整段重算 qfq 历史** → 发布新的不可变 factor dataset 与新的不可变 qfq dataset(新 dataset_id)→ 旧 dataset 全保留,旧回测继续绑定旧 dataset。内容寻址保证未变股票的 blob 零复制。禁止只追加当日 qfq K 线而不重算受影响历史(anchor 变化会使全历史比例失效)。

## 6. 回测接线与缓存隔离

- Repository L1 路径扩展:`signal_data_source ∈ {tdxquant, tushare, internal}`;任务创建时锁定 signal `dataset_id` 与 `execution_dataset_id`,运行中不重解析;回测期 Provider 调用=0(含 Tushare;断网/无 token 不影响既有 dataset 回测)。
- 任务/缓存 Hash 新增并透传:`raw_parent_dataset_id、factor_parent_dataset_id、formula_version、anchor_policy`(信号缓存键与执行缓存键均含),叠加既有 signal_data_source/adjustment/dataset_id/weekly_bar_mode/execution 双字段——只换 factor 父集必然产生不同 key 与 run。
- SQLite runs 表新增 `signal_raw_dataset_id`/`signal_factor_dataset_id` 列(v2 迁移幂等);run_meta/结果详情携带 raw_dataset_id/factor_dataset_id/signal_formula_version。
- 页面:信号源新增"本地行情+Tushare因子前复权"(无 ready 派生集时禁用);Tushare 原生 QFQ 标记为校验模式;L2 执行区只读展示 local_vendor/none+dataset 详情;legacy 旧链路显式标识,不作为任何缺数据情形的 fallback(缺 dataset → 400)。

## 7. 与后续 Gate 的关系

- 本轮只判定 READY_FOR_TUSHARE_FACTOR_SIGNAL;幸存者偏差(Gate B)与 TdxQuant/front(Gate C 第二阶段)未解决,两个上级判定保持 false。
- TdxQuant 正式同步前检查结论见 `tmp/tdxquant_pre_sync_readiness.json`:CLI/外部根/ready 发布/Provider 隔离就绪,缺 (tdxquant,front,1d) 作用域锁与 checkpoint,建议按本轮 factor 模式补齐后再全量。
