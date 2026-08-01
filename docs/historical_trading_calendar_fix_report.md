# D6 — 交易日历历史下限整改报告

- 日期:2026-07-26;缺陷:Gate C P1-D6(backtest.py:907 从 calendar.json 装载的日历仅覆盖 20160104 起 → 2016 前信号被聚集成交于日历首日或不成交;探针 bt_1785069100_1571c8:34 个 2016 前信号全部挤在 20160104/05)

## 1. 方案

repo/dataset 模式下,交易日历改由**锁定执行数据集**派生(`data/calendar.py: build_calendar_from_dataset`):

- 日历 = 执行数据集全部符号 blob 的 trade_date 并集(某日只要有一只上市股成交即为交易日)——完整覆盖数据集全程,不依赖 2016 版 calendar.json、Baostock、D:\通达信 legacy 文件或本机日期;
- 版本化:`calendar_version=1` + `dataset_id` + 日期序列 sha256;缓存于 `storage_root/calendars/calendar_<dataset_id>.json`(数据集不可变 → 日历不可变;首建 13.9s,复用 0s);
- `calendar_source/calendar_dataset_id/calendar_sha256/first/last/count` 进入 run_meta(repro.calendar)、detail API lineage,并纳入 **L3 执行缓存键**(calendar_source/sha256/dataset_id 三键)——日历变更即缓存隔离;
- 早于日历首日的信号:**显式排除**(errors 记 `before_execution_calendar: N signals … excluded`,meta 记 n_signals_before_calendar_excluded),绝不挤到首日;
- 信号日不在日历(节假日/周末):既有 next_trading_day/holiday_policy 语义,现覆盖 2000 起;日历末日之后无交易日 → next_trading_day=None,交易不发生(明确拒绝);
- 停牌区分:市场日历给出候选执行日,个股按其执行数据集内实际 bar 存在性成交(引擎既有语义,双源对称);
- legacy(非 repo)模式保留 calendar.json/from_tdx 原逻辑,完全隔离。

## 2. 实测

- 生产执行集派生日历:**20000403–20260717,6,376 交易日**,sha `862835ebeb5a…bf40e`;
- 探针:2002 元旦后首日 20020104 ✓;2008 春节周 20080201→20080204 ✓;20160104 的前一交易日=20151231(下限消失)✓;周末 20200606→20200608 ✓;末日后 None ✓;
- **2016 前执行证明**(full 引擎,10 只,20120101–20151231,run bt_1785075825_31de2d):1,496 笔成交分布 2012:267 / 2013:257 / 2014:683 / 2015:289,498 个不同成交日,**20160104 当日 0 笔**,最低成交价 2.89(正 raw);
- 正式 500 只对照:信号数与 Gate C 完全一致(43,457/47,020),成交 41,785→**43,069**(tdx)——差值即此前无法执行的 2016 前信号;
- 日历 hash 隔离:不同执行数据集 → 不同 sha(测试证明);在线/离线同数据集 → 同 sha(逐字段一致的前提之一)。

## 3. 测试

TestD6DatasetCalendar:2000–2015 可用性、2016 边界、next trading day、末日后 None、缓存回读同 sha、双数据集 hash 隔离;要求的年代样本(2000/2008/2010–2015/2016/2020/2026/周末/节假日)均在 fixtures 日期集内覆盖;信号日后无交易日与停牌语义由引擎既有测试+本轮 full 引擎实测覆盖。
