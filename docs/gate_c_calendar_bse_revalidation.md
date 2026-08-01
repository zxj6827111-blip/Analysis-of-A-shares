# Gate C 复验专项：D6 历史交易日历 + D7 北交所（独立复验）

- 证据：`tmp/gate_c_historical_calendar_evidence.csv`（1418 行逐日成交分布）、`tmp/gate_c_bse_trade_evidence.csv`（40 行逐只双源）、`tmp/gate_c_revalidation/d6_summary.json`、`d7_summary.json`、`d3_summary.json`

## D6 历史交易日历

**来源与范围**：repo 模式日历派生自锁定执行集 localvendor_none_1d_20260726_7089dc09c3c0（calendar_source=execution_dataset），20000403–20260717 共 6376 交易日，sha256 862835ebeb5a…；无周末、已知节假日（20081001/20120102/20200127/20240101/20241001）全不在册；不依赖 calendar.json 下限/D:\通达信/Baostock/网络/本机日期。

**六窗口回测**（全部经真实产品路径）：

| 窗口 | run | 状态 | 成交 | 首/末成交 | 20160104 成交 |
|---|---|---|---|---|---|
| 2000–2005 | bt_1785078399_fdde58 | unsupported_corporate_action(D8口径) | 2409 | 20000404/20051230 | 0 |
| 2008 | bt_1785078405_c472d1 | ok | 133 | 20080307/20081231 | 0 |
| 2010–2015 | bt_1785078408_0139fd | ok | 1052 | 20100330/20151130 | 0 |
| 2016边界 | bt_1785078412_f7b34f | ok | 42 | 20160112/20160329 | 0 |
| 2020 | bt_1785078415_b1dbfc | unsupported_corporate_action | 505 | 20200527/20201231 | 0 |
| 2026 | bt_1785078419_5387ca | ok | 22 | 20260326/20260717 | 0 |

- 2012–2015 逐年成交：118 / 218 / 407 / 193 —— **2016 前正常分布，无 20160104 聚集**
- 早于日历首日：窗口 19950101–19991231（bt_1785080053_b7d0f2）→ n_events=0、fills=0，显式排除 ✅
- 数据末端无后续交易日 → 不成交（no_fill_after_last_calendar_day=true）✅
- 停牌语义：600256@2018 市场日 243 vs 个股日 230（停牌 13 日），个股可交易日与市场日历区分 ✅
- 日历三键（source/dataset/sha）入 run_meta.repro.calendar 与执行缓存键；detail API 四 run 均返回 862835ebeb5a ✅

## D7 北交所 Repository 模式

**代码段映射**（生产函数实测）：43/83/87/920 裸码与 bj 前缀全部 → BSE.STK.*；920xxx 不误判 SSE B 股（900901 非 BSE）；`_symbol_variants` 覆盖 920001.BJ/bj920001/920001 ✅（后缀形式在 repository 层解析，属契约内分层）

**段普查**：

| 数据集 | BSE 总数 | 43 | 83 | 87 | 920 |
|---|---|---|---|---|---|
| tdxquant_front（信号A） | 328 | 0 | 0 | 0 | 328 |
| internal_tsfqfq（信号B） | 329 | 0 | 0 | 0 | 329 |
| localvendor_none（执行） | 571 | 15 | 173 | 54 | 329 |

- 共同 BSE 池 = **328**（全 920 段）；排除 243 只**逐只有因**（43/83/87 段历史/退市实体不在信号集——TDX 不服务退市股，与幸存者偏差披露一致）
- 迁移关系：328 只中 **238 只** 920 代码下历史早于 2024（迁移前历史随码延续），如 920000 首日 20201223
- 双 variant 实验：A 档实验含 20 只 BSE 全部随双 variant 成功（排除 0）；页面共同池横幅一致
- 逐只成交证据（直连产品路径 fills.csv）：tdx 源 178 笔/11 只有成交、internal 源 214 笔/18 只，价格全正、price_source=raw；无个股硬编码、无整板块静默剔除
- 计数器：Baostock=0、Provider=0、TdxDayReader=0、非回环网络=0（在线离线两服务器全程为 0）

## D3 缺行情与零信号五态（附）

| 态 | 语义 | 实测 |
|---|---|---|
| 不在 dataset | 预检 400 SYMBOL_NOT_COVERED (signal_not_in_dataset) | BSE.STK.430047 ✅ |
| manifest 有/blob 缺 | 提交时 422 DATASET_CORRUPT + 读取期 FileNotFoundError 硬失败（隔离库） | ✅ |
| 区间无行情 | 200 + errors 显式 no_data_in_range（signal 与 execution 双标注） | 920000@2005 ✅ |
| no_data allowlist | 预检 400 SYMBOL_NOT_COVERED (signal_no_data_allowlisted) | SSE.STK.600193 ✅ |
| 真零信号 | 200 成功 n_signals=0 无 error | 600000@20240603-14 ✅ |

五态语义互斥、页面/API 不混同。
