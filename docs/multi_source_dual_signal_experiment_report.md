# 多信号源 Gate C — 双信号源产品实验报告

- 日期:2026-07-26;分支 `feat/multi-source-market-data`;HEAD `8ee03196aa20a4ce10f8674ead55dd34b33867e9`(冻结)
- 证据:`tmp/multi_source_dual_signal_experiment.json`、`tmp/gate_c_final/exp_*.json`、`tmp/multi_source_offline_acceptance.json`、`tmp/multi_source_trade_evidence.csv`

## 0. 产品路径形态(重要前置结论)

产品内置的"双源对照"开关(`dual_source_compare`,前端复选框"开启(通达信 vs Tushare)")**硬编码解析 `(tdxquant,front)+(tushare,qfq)`**(experiments.py:1002-1040),无法表达本轮要求的信号 B(internal/tushare_factor_qfq)。实测经产品 API 提交返回 400:"双源对照实验需要 ready 数据集,但以下来源缺少 ready dataset: tushare"。→ **缺陷 D2(P0),单实验双 variant 的字面要求判 FAIL**。

因此本轮以**等价产品路径**执行:每个信号源各建一个真实实验(同一 REST 入口 `POST /api/v1/experiments` → `/start` → variants → BacktestRequest → Repository → 回测 → 缓存 → SQLite(variants 表)→ 结果 API → 页面),两实验除 signal 三元组外全部参数逐字段相同(同规则、同池、同窗、同买卖日程、同止盈止损、同费用、同滑点、同初始资金、同 L2 执行集)。未手工调用内部函数伪造 variant。

## 1. 实验配置

- 策略:`tn6_735金叉及趋势`(MA7/MA35 金叉+双升趋势+DEV≤2 回踩;paired_confirmed 人工确认公式)
- 日程:T+1 开盘买,持有 5 交易日开盘卖;止损 3%,止盈 8%;portfolio 共享资金;费用 佣金 0.03%(最低 5 元)+印花税 0.1%;滑点 0;初始资金 1,000,000
- L2 统一:`localvendor_none_1d_20260726_7089dc09c3c0`(local_vendor/none,raw)
- 共同截止:**20260717**(三集最小截止;TdxQuant 的 20260718–20260724 尾段未被使用)

### A 档(长周期代表性,500 只)
- 池:共同池∩因子缓存 分层抽样(沪主板 160/深主板 140/创业板 130/科创板 70;**北交所 0** —— 产品 L3 复权因子门禁依赖 baostock,无北交所数据且缓存为空,正式/离线模式无法运行 BSE,系产品级限制,另以专项探针记录)
- 窗口:20120101–20260717(注意:产品交易日历下限 **20160104**,2016 年前信号仅能聚集成交于日历首日——探针 bt_1785069100_1571c8 证实 34 个 2016 前信号只在 20160104/05 成交;双源对称)

### B 档(全共同池规模,4,962 只)
- 池:共同池 5,528 ∩ 因子缓存 complete = **4,962**(排除 BSE 328 + 因子缓存缺失 238,原因逐股记录于 universe CSV)
- 窗口:20240101–20260717;主要验证规模/调度/缓存/落库/页面

## 2. 运行结果(在线)

| 实验 | experiment_id | variant | run_id | 状态 | 信号 | 成交(fast筛选) | CA拦截 |
|---|---|---|---|---|---|---|---|
| A/tdxquant | exp_b487c49cfe | v000 | bt_1785068590_1a3fde | succeeded | 43,457 | 41,785 | 676 |
| A/internal | exp_74e9864132 | v000 | bt_1785068665_0446dd | succeeded | 47,020 | 45,164* | — |
| B/tdxquant | exp_b9f15205f9 | v000 | bt_1785069155_b7b190 | succeeded | 117,517 | 114,078 | 2,696 |
| B/internal | exp_304ea52ea4 | v000 | bt_1785069570_2b5302 | succeeded | 120,918 | 117,342 | 2,821 |

(*A/internal n_trades 见 metrics.json)四个 run_id 互不相同;每对实验 execution_dataset_id 完全相同;lineage(runs_index/run_meta)与请求 dataset 严格一致,B 侧记录 raw 父=localvendor…7089dc09c3c0、factor 父=tushare_adjfactor…acc8d3cadc79、公式 tsqfq_v1。

补充 full 引擎对照(直连 `POST /api/v1/backtests`,artifact_level=full,产 fills):clean30(2024 年,30 只):A 276 信号/268 成交,B 294/278;负价年代 5 只(2018–2021):A 567/461,B 180/151。交易证据 72 行见 `tmp/multi_source_trade_evidence.csv`。

## 3. 硬要求核对(十二节)

1. 实验真实生成 variant ✓(单实验双 variant ✗ → D2,采用等价双实验);2. run_id 互异 ✓;3. signal dataset 互异 ✓;4. execution dataset 完全相同 ✓;5/6. A 只读 tdxquant 集、B 只读 tsfqfq 集 ✓(lineage+缓存键+信号数装载吻合);7. 双方 L2 同一 local_vendor 集 ✓(131 笔共同买入 raw 价 0 差异);8. Provider 调用=0 ✓;9. TdxDayReader=0 ✓;10. 未读原始 ZIP ✓;11. 实验路径网络调用=0 ✓(在线全程仅 1 次外联为页面 bagua/gua 族 legacy 端点取 002008 分红事件,代码路径证明与 repo 模式实验无关;离线复跑 0 网络);12. 不重复 resolve ✓(显式 dataset_id 锁定);13/14. 单 variant 失败→实验失败计数可见(前提:variants 表工作正常;但注意 D5 使 runs 表缺失)△;15. 结果页区分两口径 ✓(tdxquant 标签+负价 tooltip;internal 标签+raw/factor 父集展示)。

## 4. 离线复跑(十七节)

离线服务器(:8766):非环回 socket 即抛、全 Provider/tqcenter/TdxDayReader/供应商 ZIP/baostock 即调即抛并计数、Tushare token 清除、独立沙箱 storage/SQLite/缓存(冷),共用只读生产数据根。

| 复跑 | experiment/run | 状态 | 与在线逐字段比较 |
|---|---|---|---|
| A/tdxquant | exp_75a8dff2d4 / bt_1785069834_9ed253 | succeeded | 22 字段 **0 差异** |
| A/internal | exp_08ed466624 / bt_1785069881_bdd21e | succeeded | 22 字段 **0 差异** |
| B/tdxquant | exp_221c68d58a / bt_1785070258_49720e | succeeded | 22 字段 **0 差异** |
| B/internal | exp_ff2b865566 / bt_1785070620_59f30f | succeeded | 22 字段 **0 差异** |
| clean30 A/B(full) | bt_1785070659_ff766e / bt_1785070660_77e1fd | 同在线 | 39 字段各 1 个最后一位浮点摆动(绝对差 ~3e-11) |

离线计数器:provider=0,tdxday=0,tqcenter=0,baostock=0,外联尝试=0。**离线复跑通过。**

## 5. 缓存隔离(十四节)

- A/tdx 冷启动 miss → 43,457 信号计算并缓存;A/internal miss(**未串用 A/tdx 缓存**)→ 47,020 独立缓存;
- A2(同配置仅止损 0.03→0.04):signal_cache_hit=**True**(L1 键不含执行参数,合规复用),execution cache miss(止损在 L3 键内);
- A3(完全同配置):signal+execution cache 双命中(安全复用);
- 缓存键实测含 data_source/adjustment/dataset_id/raw_parent/factor_parent/formula_version/anchor_policy/weekly_bar_mode/anchor_date/execution_data_source/execution_dataset_id/universe_sha/日期/指标源哈希(backtest.py:506-536;backtest_context.py:201-245);legacy 键位全空,新旧不互认;
- 注意:SQLite param_hash 去重复用因 D5 失效(A3 期望 skipped-复用,实际新建 run)。
