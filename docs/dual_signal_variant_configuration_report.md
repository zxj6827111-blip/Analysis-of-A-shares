# D2 — 双源对照实验配置化整改报告

- 日期:2026-07-26;缺陷:Gate C P0-D2(experiments.py:1002-1040 硬编码 (tdxquant,front)+(tushare,qfq),无法表达 internal/tushare_factor_qfq,单实验双 variant 不可达)

## 1. 设计

- **`signal_variants` API 字段**(POST /api/v1/experiments):显式 variant 列表,每项 `{signal_data_source, signal_adjustment, dataset_id?}`;≤4 项、去重、缺省 adjustment 按 SIGNAL_SOURCE_ADJUSTMENT 补全;**legacy 源禁止入列**(显式报错,绝不作第三个自动 fallback variant);
- **正式模板** `DUAL_SOURCE_COMPARE_TEMPLATE = [(tdxquant,front),(internal,tushare_factor_qfq)]`(模块级常量):`dual_source_compare=true` 仅作该模板的别名展开,与用户显式列表走**同一条代码路径**——不再有任何字符串特判固定数据源;显式 signal_variants 恒优先;
- 每个 variant 数据集经 D1 绑定校验(显式 id)或 resolve_latest_ready(隐式),要求各 variant signal dataset 互异;
- 执行数据集全实验唯一(显式校验或按 execution_data_source/none 解析),所有 variant 强制共用;
- 单源 repo 实验走同一解析器(1 个描述子),行为兼容且获得同样的覆盖预过滤。

## 2. 共同池与共同截止(§八,全动态,零硬编码)

- `common_universe = requested ∩ (∀ signal manifest 有效符号) ∩ execution 有效符号 ∩ (∀ CA 因子集覆盖)`;逐股 exclusion_reason(signal[label]:not_in_dataset / no_data_allowlisted / ca_factor_* / execution:*),各源独立计数;
- `effective_end = min(各 signal manifest max(last_date), execution max(last_date), 用户 end)`;
- config 落库:requested_universe_count / common_universe_count / excluded_by_signal_counts / excluded_by_execution_count / excluded_total / exclusions(≤500 条逐股)/ requested_end_date / dataset_common_cutoff / effective_end_date + signal_variants(含 raw/factor 父集与 formula_version)+ execution 摘要;
- 所有 variant 强制同池(codes=common)、同 effective_end、同策略/日程/止盈止损/费用/滑点(网格轴本就同源)、同执行集。

## 3. 失败语义

- `_run_experiment` 终态:`failed_variants>0 → 实验 status="failed"`(绝不 completed/succeeded);cancelled 优先;
- experiment_variants 正确落库(创建即 pending 行,运行中 running/succeeded/failed + run_id/error);
- 结果 API 行新增 signal_data_source/signal_adjustment/signal_dataset_id/execution_dataset_id 便于页面直读。

## 4. 实测(单实验双 variant,真实产品路径 POST /experiments → /start → variants)

| 实验 | 池 | 结果 |
|---|---|---|
| exp_3290cb2129(小规模) | 33 只(6 BSE+5 负价),2012→eff 20260717 | v000 tdx=bt_1785075329_a56535,v001 int=bt_1785075334_722020,均 succeeded |
| exp_2a1d441663(BSE20) | 20 只北交所 | 双 succeeded(287/89、107/107) |
| exp_b0ef63c8c4(正式) | 500 只 2012–20260717 | 双 succeeded(43,457/47,020 信号;43,069/46,585 成交;93s) |

同一 experiment_id、两个 variant_id、两个互异 run_id、signal dataset 互异、execution dataset 相同、池与日期逐字段相同、SQLite/history/detail 全通;离线复跑 132 metrics 键 0 差异。probe10 证明 variant 失败→实验 failed;probe11 证明废弃 (tushare,qfq) 对不再被隐式解析。

## 5. 页面(G1)

实验结果页动态横幅:请求/共同池数量、各源+执行排除计数、三重截止、L2 执行集、每 variant 的 signal dataset(+raw/factor 父集);结果行 variant 标签含 `信号:source/adjustment@dataset`。

## 6. 测试

TestD2SignalVariants(单实验双 variant/共同池与截止/模板/legacy 禁入/variant 失败→实验 failed)+ test_multi_source_integration 双源用例更新为新模板语义。
