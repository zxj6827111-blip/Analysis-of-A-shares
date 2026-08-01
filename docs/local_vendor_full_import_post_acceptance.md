# 供应商全量 dataset 导入后验收报告

**判定: `PASS` — LOCAL_VENDOR_FULL_DATASET_READY=true / READY_FOR_VENDOR_EXECUTION_BASELINE=true**

| 项目 | 值 |
|---|---|
| 日期 | 2026-07-26 |
| dataset | `localvendor_none_1d_20260726_7089dc09c3c0`(ready) |
| 保持 | READY_FOR_SURVIVORSHIP_SAFE_BACKTEST=**false**;READY_FOR_MULTI_SOURCE_PRODUCTION_BACKTEST=**false** |
| 证据 | tmp/local_vendor_full_import_post_acceptance.json + quality_issues.csv + raw_comparison.csv |

## A. 全量结构检查(5796 只逐一,numpy 向量化,11.8s)

| 检查 | 结果 |
|---|---|
| Repository 可读取 | 5796/5796 ✅ |
| 日期严格升序 / 无重复 | **0** 违例 ✅ |
| OHLC 非空且 >0 | **0** 违例 ✅ |
| blob 存在 / SHA256=内容 | 5796/5796 全部重算验证 ✅ |
| manifest 统计 vs 实际 | 行数重算 16,046,025 = manifest;symbol/imported 计数一致 ✅ |
| symbol-交易所一致 | **0** 违例;unknown_symbol=**0** ✅ |
| high≥low / low≤open,close≤high | ⚠️ 22 只 25 行例外(见 C 节结论) |

**唯一例外——供应商源数据自带异常(非导入缺陷)**:21 只北交所股票集中在 **2022-10-31 同一交易日**(收盘价越出当日高低区间,如 430139 C13.5>H13.07)+ SZSE.000901 四行。抽检 6/6 行与原始 CSV **逐值一致** → 导入按 `adjustment=none` 契约忠实保留原始值;占比 25/16,046,025 = 0.00016%。已逐行记录于 [quality_issues.csv](../tmp/local_vendor_full_import_quality_issues.csv);下游使用该日北交所数据时应知悉。若未来决定清洗,须以版本化质量规则显式处理,不得静默改值。

## B. 日期与覆盖

- 股票 5796 / K线 16,046,025 / 20000403 ~ 20260717
- 年度股票数:873(2000)→ 1518(2009)→ 3562(2019)→ 5360(2024)→ 5547(2026);年度K线数 131,473 → 708,046(2026 半年)
- 板块:沪主板 1710 / 科创 611 / 深主板 1500 / 创业板(30x)1404 / 北交 571
- 最新年缺席 249(=宇宙疑似退市数,自洽);no_data=0;failed=0

## C. 原始数据抽检(200 只分层)

分层:沪主板 40 / 深主板 40 / 创业板 30 / 科创 25 / 北交 30 / 最新年缺席 15 / 2000 年长史 10 / 2024+ 新股 10(强制含 000901)。每只取首/中/末 3 个年份×随机日期,共 **1,133 行**:

| 对比 | 误差 |
|---|---|
| open/high/low/close 逐值 | **0** |
| volume(手→股 ×100) | **0** |
| amount(千元→元 ×1000) | **0** |
| pre_close | 逐行记录;12 只样本存在公司行为日(pre_close≠前收盘)——**现金分红/送转/配股案例实证覆盖** |

说明:供应商 CSV 无名称字段,ST/曾 ST 无法直接识别;以公司行为日与长停牌样本替代覆盖,如实注明。

## D. 跨年度检查

2000/2001、2009/2010、2019/2020、2025/2026 各 3 只共 **12 项:0 失败**——边界无重复日期、顺序正确、年末→年初间隔正常(全量扫描亦证明 0 重复日期,同日冲突由 trade_date 去重规则处理)。

## E. Repository 与 resolve

- `resolve_latest_ready(local_vendor,none,1d)` → **新全量 dataset** ✅
- 5 个历史 manifest 全保留(2 partial 永不被选;107 只/500 只试点 ready 保留)✅
- 6 种 symbol 格式(SSE.STK.600000 / 600000.SH / sh600000 / 600000 / SZSE.STK.000001 / BSE.STK.430047)全部可读 ✅
- 错误交易所(SZSE.STK.600000 / SSE.STK.000001 / SZSE.STK.999999)全部 DatasetNotFoundError ✅
- 全程未读原始 ZIP、未调用 LocalVendorProvider;**Provider 调用=0**(5 类 Provider + TdxDayReader 插桩)✅

## F. API 与页面

API `/api/v1/market-data/status`(production env,TestClient 实测):data_root=正式根、is_test_root=false、astock_env=production、ready/partial/failed=3/2/0、总量 331.9MB;`latest_local_vendor` = 新 dataset(5796 只 / 16,046,025 行 / 20000403~20260717 / **survivorship_bias=true** / warning_text 非空)。页面数据仓库面板据此渲染:红/橙**警告横幅**(固定文案"该数据集缺少部分历史退市股票,长期全市场回测存在幸存者偏差。")、最新 local_vendor 行、日期范围列、"偏差"标记(Gate A 已 23 断言冒烟,本轮 API 字段实测就绪)。无任何"完整历史无偏"表述。

执行链路:`execution_data_source=local_vendor` → 解析 `execution_dataset_id=localvendor_none_1d_20260726_7089dc09c3c0`;完整多信号源回测留待 Gate C(正式根尚无 TdxQuant/Tushare 信号 dataset)。

## G. 默认 pytest 复跑

`python -m pytest -q`(干净环境,不带 --ignore=tmp):**631 passed / 1 skipped(live_tdxquant 客户端离线)/ 0 failed** —— 测试未被正式数据根污染(全部走隔离临时目录)。

## H. 保留与清理

原始 ZIP、universe CSV、sync log、新旧 ready、试点 dataset、验收报告全部保留;本次导入 temp 目录增量为 0,无需清理;checkpoint 按设计随成功发布消费(sync_log 留存审计)。
