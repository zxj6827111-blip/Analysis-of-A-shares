# internal/tushare_factor_qfq 派生报告

| 项目 | 值 |
|---|---|
| 日期 | 2026-07-26 |
| 判定 | **PASS — TUSHARE_FACTOR_QFQ_DATASET_READY=true** |
| **派生 dataset_id** | **`internal_tsfqfq_1d_20260717_c962acb8af26`**(ready) |
| raw 父集 | `localvendor_none_1d_20260726_7089dc09c3c0`(manifest sha 已记录) |
| factor 父集 | `tushare_adjfactor_1d_20260726_acc8d3cadc79`(manifest sha 已记录) |
| cutoff / anchor / formula | 20260717 / `last_factor_on_or_before_cutoff` / **`tsqfq_v1`** |
| 命令 | `python scripts/sync_market_data.py --source internal --mode derive --adjustment tushare_factor_qfq --raw-dataset-id … --factor-dataset-id … --cutoff 20260717` |

## 派生结果

| 指标 | 值 |
|---|---|
| eligible(raw∩factor) | 5554 |
| **imported** | **5554(100%)**,failed=0 |
| excluded | 242(全部 `no_factor`,北交所缺席股) |
| K 线 | **15,562,445**(20010427 ~ 20260717) |
| 耗时 | **41.7s**(纯 numpy 数组管线 + store_bar_arrays) |
| 已知问题继承 | 49 只前导缺口丢行(raw 早于因子首日,逐笔记录 issues);OHLC 边界异常 2 只继承自 raw 源(标注 `raw_source_inherited`,非因子问题) |

## 计算规则(tsqfq_v1)

`ratio(t) = adj_factor_asof(t) / anchor_factor`;asof 只用**当日或过去**因子(searchsorted-right),前导缺口行丢弃并计数——绝不用未来因子回填;qfq O/H/L/C = raw × ratio,银行家舍入 4dp;**volume/amount 原样继承 raw(股/元),绝不乘价格比例**;bar schema 无 pre_close,规则记入 provenance(下游=前一 qfq close)。停牌/退市/北交所/非交易日 cutoff/因子≤0/重复因子等 14 项边界全部实现并被 39 项新测试覆盖。

## 全量质量验收(tmp/_gc_validate.json + tushare_factor_quality_issues.csv)

| 检查 | 结果 |
|---|---|
| 5554 只结构逐检(升序/去重/正价/blob SHA 重算) | **全部通过,0 例外** |
| 行数重算 = manifest | 15,562,445 = 15,562,445 ✅ |
| **ratio 抽核**(300 只随机日,由因子 blob 独立重算) | **300/300 完全一致** |
| **anchor 恒等式**(末段 ratio=1 ⇒ 派生价=raw 价) | 5548/5554 成立;6 只偏离**全部证实为合法**:末次交易日后停牌期间发生公司行为(如国华退/恒久退/赛隆退),cutoff 锚定语义正确 |
| 血缘完整性 | raw/factor id+sha、formula、anchor、universe_sha256 全在 manifest ✅(上一轮 local_vendor manifest 缺失的 universe_sha256/单位策略等,本轮新集全部补齐,未回写旧不可变 manifest) |

## 增量策略实证(tmp/_gc_incremental_rehearsal.json)

临时环境三轮演练 PASS:历史因子修订(1.2→1.25)→ 受影响股 day1 价格 8.4167→8.08(全历史重算,手算可验);新公司行为 → anchor 变化 → 历史整段重算(10.1→9.1818);同 raw 不同 factor → 三个互异 dataset_id;旧集全保留;禁止只追加当日。

## 与 Tushare 原生 qfq 抽样对比(100 只,tmp/tushare_factor_qfq_comparison.csv)

| 指标 | 值 |
|---|---|
| 对比范围 | 100 只(沪主板18/深主板18/创业板14/科创10/北交14/长史6/新股6/缺席5/Tushare退市5/多次公司行为10),29,056 交易日,116,224 个 OHLC 单元 |
| **两位小数一致率** | **99.55%** |
| 最大绝对误差 | **0.005**(=2dp 舍入边界;Tushare 输出仅 2dp,我方存 4dp) |
| 最大相对误差 | 3.46%(低价股上 0.005 的放大,同为舍入级) |
| 锚点不匹配股票 | **0** |
| 日期覆盖差异 | 单边仅存在于 Tushare 20,681 天(=前导缺口股 + 北交所股票的新三板时期行情,vendor 无;派生侧 0 多余日期);逐类说明,未修改任何 raw 价格 |
| 差异 Top50 | 全部为 2dp 舍入级,无结构性偏差;近公司行为日无特异放大 |

**结论**:差异完全由输出精度(2dp vs 4dp)与数据源日期覆盖构成;"本地行情+Tushare因子"前复权与 Tushare 原生 qfq 在 2dp 口径实质等价。
