# 多信号源正式产品 Gate C 最终独立验收报告

- 验收日期:2026-07-26(独立验收,只验收不开发;生产代码零修改)
- 分支:`feat/multi-source-market-data`;HEAD:`8ee03196aa20a4ce10f8674ead55dd34b33867e9`
- 工作区:冻结的未提交开发状态(12 个修改文件+未跟踪文档),指纹 `FREEZE_DIFF_SHA256=f6d766b2cbaa5d55739ad59c7029bdd855e2a982775831477a3c67c2f48bd6fc`(2,215 行 diff);验收结束复核一致,快照有效
- 数据根:`E:\AStockData\datasets\market_data`(production,external,env 显式设置)

## 最终判定

# **FAIL**

- **READY_FOR_MULTI_SOURCE_PRODUCTION_BACKTEST = false**
- **READY_FOR_SURVIVORSHIP_SAFE_BACKTEST = false**(维持;历史退市股未补齐)

数据面与执行面质量全部达标(两套信号集与执行集 ready、共同池明确、L1/L2 严格分离、缓存严格隔离、离线复跑逐字段一致、负价语义受控),但存在 **3 个 P0 级产品代码缺陷**(D1/D2/D5),分别击穿硬门槛第 6、14、15、18、20 项。按验收规则"发现代码缺陷只记录证据、判定 FAIL、不当场修复"执行。

## 硬门槛计分(二十节)

| # | 项 | 结果 | 证据 |
|---|---|---|---|
| 1 | 默认 pytest 通过 | ✓ | 702 passed/0 failed/0 skipped,114.39s,exit 0;live_tdxquant 实连实跑;tmp 未被收集(pytest.ini norecursedirs) |
| 2 | 双信号集 ready | ✓ | manifests:tdxquant_front…09b179b48611(5,547 收录/5,528 有数据/16,651,526 行)、internal_tsfqfq…c962acb8af26(5,554/15,562,445) |
| 3 | 执行集 ready | ✓ | localvendor_none…7089dc09c3c0(5,796/16,046,025,min 价 0.10>0) |
| 4 | 共同股票池明确 | ✓ | **5,528**(=tdx∩tsfqfq∩exec;tdx⊂两者);仅tsfqfq 26;双缺-exec有 242(全 BSE 43x);CSV 逐股口径 |
| 5 | 共同日期范围明确 | ✓ | 共同截止 **20260717**;TDX 尾段 0718–0724 未用;**执行有效下限 20160104(交易日历,D6)** |
| 6 | 双源 variant 经产品路径生成 | **✗** | **D2**:dual_source_compare 硬编码 (tdxquant,front)+(tushare,qfq),无法表达 internal/tsfqfq;实测 400。以两个真实产品实验等价替代 |
| 7 | 两 variant 均成功 | ✓ | A/B 档在线+离线共 8 个 variant 全 succeeded,run_id 互异 |
| 8 | 同一 execution dataset | ✓ | 全部 run execution_dataset_id 相同;131 笔共同买入 raw 价 0 差异 |
| 9 | L1/L2 严格分离 | ✓ | 72 行交易证据;负价 L1 信号 416 个 → 成交 raw 最低 2.61,0 笔非正价 |
| 10 | Provider 调用=0 | ✓ | 在线/离线计数器均 0(tdxquant/tushare/local_vendor/tqcenter) |
| 11 | TdxDayReader=0 | ✓ | 计数器 0(repo 模式无该构造点) |
| 12 | 离线回测成功 | ✓ | 4 实验离线 succeeded;fast 档 4×22 字段 0 差异;full 档 2×39 字段仅 last-ulp 浮点摆动(~3e-11) |
| 13 | 缓存不串用 | ✓ | A/int 未命中 A/tdx 缓存;A2 合规命中;A3 全命中;键含全部要求字段;legacy 键隔离 |
| 14 | SQLite 正确落库 | **✗** | **D5**:存量生产库 schema v2 缺 signal_raw/factor 列且版本号未升→永不迁移;upsert 异常被静默吞(runs.py:100-107)→**今日全部 run 未入 runs/metrics 表** |
| 15 | API 与页面完整追溯 | **✗** | D5 连带:history API(SQLite 优先)看不到今日 run;experiments/variants 表、runs_index.json、run_meta.json、详情 API、export.xlsx 均正确;页面不显示锁定信号 dataset_id(G2) |
| 16 | 负价明确处理 | ✓ | 专项报告 PASS(tooltip 级警告 W1、无策略负价标记 W2 为弱面) |
| 17 | 覆盖差异明确处理 | ✓ | universe CSV 逐股 exclusion_reason;26/242/BSE/最新年度缺席全量化 |
| 18 | 失败语义正确 | **✗** | **D1**:显式 dataset_id 时 source/adjustment 与 manifest 不匹配不校验(19.6/19.7),错配 run 以 status=ok 落盘且 lineage 污染(bt_1785068394_78b479/_4417b3);另 D3(白名单缺数股静默零信号)、D4(不存在 dataset→HTTP 500) |
| 19 | legacy 不 fallback | ✓ | 缺集探针全 400 且无回退;legacy 需显式选择 |
| 20 | 无新 P0 | **✗** | D1、D2、D5 |

其余拒绝语义(partial 签名集/partial 执行集/缺 tdxquant 变体/缺 internal 变体/缺执行集)5 项全部正确 400 拒绝、无静默 fallback。

## 缺陷清单(本轮新发现,仅记录未修复)

| 编号 | 级别 | 缺陷 | 证据 |
|---|---|---|---|
| D1 | P0 | 显式 dataset_id 绕过 source/adjustment-manifest 一致性校验;错配请求正常运行并以污染 lineage 落盘 | backtest.py:187-193;runs bt_1785068394_78b479(声称 internal/tsfqfq 实读 tdxquant 集)、bt_1785068394_4417b3 |
| D2 | P0 | dual_source_compare(前端"通达信 vs Tushare"复选框)停留旧设计,无法生成要求的 A/B 对;单实验双 variant 不可达 | experiments.py:1002-1040;API 400 实测;index_v3.html:1841 |
| D5 | P0 | SCHEMA_SQL/迁移新增 signal_raw/factor_dataset_id 两列但 _SCHEMA_VERSION 未升(仍=2),存量 v2 生产库永不获列;upsert 失败被 try/except pass 吞→新 run 全部不入 SQLite runs/metrics,history API 缺失今日 run,param_hash 去重复用失效 | db.py:21/65-72/174-202;runs.py:100-107;实测 runs 表 71 行全为旧 run |
| D6 | P1 | 交易日历 calendar.json 仅覆盖 20160104–20260717:2016 前信号全部聚集成交于 20160104/05 或不成交,长周期实验执行面实际从 2016 起 | backtest.py:907;探针 bt_1785069100_1571c8(34 信号→仅 20160104/05 共 4 笔) |
| D7 | P1 | 正式模式 L3 复权门禁依赖 baostock 因子(repo 模式仍逐股 build_factor_series),北交所无 baostock 数据且缓存 0 覆盖→BSE 股票无法进入正式/离线回测;共同池 5,528 中仅 4,962 只可正式运行 | backtest.py:319/598;adjustments 缓存 BSE=0;universe CSV |
| D3 | P2 | 信号集内 no_data 白名单股返回空列表→静默零信号无提示(19.8"明确处理"弱) | repo.load_bars(600193@A)=[];run bt_1785068395_008e84 status=ok errors=[] |
| D4 | P2 | 不存在的 dataset_id→HTTP 500(DatasetNotFoundError 未映射 400);不在 manifest 的 symbol 同样 500(消息清晰) | 探针实测 |
| D8 | P2 | fast 档 run 详情 API 顶层 status=ok 而 metrics.status/runs_index=unsupported_corporate_action(状态口径不一致) | bt_1785068590_1a3fde |
| D9 | P2 | BSE 代码 baostock 查询误用 sz. 前缀(潜伏);select_universe 不接受"BSE.STK.*"格式入参(须 bj 前缀) | adjustments.py:382;backtest_universe.py:86-91 |
| G1 | P2 | 前端无共同股票池/共同截止日期展示;双源对照无共同池约束 UI | index_v3.html 全文检索 |
| G2 | P2 | 结果页不显示锁定的信号 dataset_id(A/B 均只显示源标签;B 显示 raw/factor 父集) | 任务详情页实测 |
| W1/W2 | P2 | 负价警告仅 tooltip 级;策略负价支持性无显式标记 | 专项报告 |
| G3 | P2 | 任务详情路由下点击侧边导航整页跳回 /v3?module=…,落点视图不定(用户报告复现) | 浏览器实测 |

## 分项证据索引

- 数据根一致性:server 启动横幅+/health+/market-data/status(production/external/6 ready/3 partial/0 failed;partial 拒选实测)
- 共同池:`tmp/gate_c_common_signal_universe.csv`、`tmp/multi_source_common_universe.csv`、`tmp/gate_c_final/universe_summary.json`
- 双源实验:`docs/multi_source_dual_signal_experiment_report.md`、`tmp/multi_source_dual_signal_experiment.json`
- 负价专项:`docs/multi_source_negative_qfq_handling_report.md`(699/719,700=close≤0 口径核准;全口径 710/733,317;零价 3,676 行)
- 差异归因:`docs/multi_source_result_difference_attribution.md`、`tmp/multi_source_result_difference.csv`
- 交易证据:`tmp/multi_source_trade_evidence.csv`(72 行)
- 离线:`tmp/multi_source_offline_acceptance.json`
- 命令:`tmp/multi_source_gate_c_commands.txt`;失败探针 `tmp/gate_c_final/failure_probes.json`

## 非阻断风险提示(维持)

供应商 raw 幸存者偏差(manifests 已标注+页面警示);历史退市股未补齐(Gate B 未过);TdxQuant 仿射负价(专项受控);双复权模型结果显著不同(已归因,非错误);北交所历史代码单源覆盖差异+BSE 正式回测不可用(D7)。

## 结论与建议

Gate C 判定 **FAIL**。数据工程面(Gate A/C-1/C-2 资产)完好且经受了独立复核;失败集中于产品编排层的三个可修复缺陷。建议修复顺序:D5(一行版本号+迁移,影响最广)→ D1(resolve 时校验 manifest.source/adjustment)→ D2(dual_source_compare 改为 (tdxquant,front)+(internal,tushare_factor_qfq) 或可配置)。修复后仅需复验:失败语义探针、SQLite/history 落库、单实验双 variant 生成与两档实验重跑(信号缓存可复用,成本低)。Gate B(退市股 composite)为数据侧工作,可与上述修复并行,不受本轮 P0 阻塞;但在 Gate C 复验通过前 READY_FOR_MULTI_SOURCE_PRODUCTION_BACKTEST 维持 false。
