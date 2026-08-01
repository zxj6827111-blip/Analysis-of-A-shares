# TdxQuant 前复权数据集质量验收报告

**判定:PASS**(对象 `tdxquant_front_1d_20260726_09b179b48611`)

## A. 全量结构与完整性(5528 只逐只,tmp/_gd6_quality.json)

| 检查 | 结果 |
|---|---|
| Repository 可读 | 5528/5528 ✅ |
| **blob SHA256 逐一重算** | **5528/5528 一致**(内容寻址完整性)✅ |
| 行数重算 = manifest | **16,651,526 = 16,651,526** ✅ |
| trade_date 严格升序且无重复 | 0 违例 ✅ |
| OHLC 非 NaN | 0 违例 ✅ |
| high≥low、open/close∈[low,high] | 0 违例 ✅(仿射变换单调性保序) |
| symbol↔交易所号段一致 | 0 违例(unknown_symbol=0)✅ |
| 逐股 first/last 日期 = manifest | 0 违例 ✅ |
| duplicate/missing/corrupt blob | 全部 0 ✅ |

**前复权负价段(原生语义,非缺陷)**:699 只 / 719,700 行 close≤0——通达信仿射前复权(现金分红减常数)在长历史高分红股早期段的客户端同款表现,逐只计入 [quality_issues.csv](../tmp/tdxquant_front_quality_issues.csv) 说明;涨跌停/成交/估值全走 L2 raw 不受影响;页面与 manifest 均有提示。

## B. 200 只实时抽检(tmp/_gd6b_live_spotcheck.json + tdxquant_front_raw_comparison.csv)

分层抽取 200 只(沪主板 55/科创 20/深主板 60/创业 35/北交 30,并强制含长史与负价段代表 000001/000002/600000/600519/601088/301107),与 TdxQuant **实时重新拉取**逐值对照:

| 指标 | 值 |
|---|---|
| 完全一致股票 | **200/200** |
| 对比单元(O/H/L/C×日) | **2,443,204,逐值相等 2,443,204(100%)** |
| 最大绝对差 | **0.0** |
| 日期集不一致 | 0 只 |

未修改任何供应商原始返回值;历史起止日期逐只一致。

## C. 单位与 raw 交叉核对(样本轮)

301107 五个交易日:TDX Volume 与 vendor 逐值相等(股,未复权);TDX Amount×10000 与 vendor 元级金额一致(比值 1.0000,万元舍入差 ≤100 元)。`volume_policy=tdx_volume_shares_as_returned_unadjusted`,`amount_policy=tdx_amount_wan_yuan_scaled_x10000_to_yuan`。

## D. 301107 基准(vendor-native)

数据集内 2026-05-11~15 日线聚合周线 = **O=20.15 H=20.25 L=19.15 C=19.65**,与通达信客户端目标值精确一致;vendor-native 周线全史拉取同值;区间起点落在目标周内的周线请求 Open 异常(19.72)已记录并排除出正式流程(不混用)。
