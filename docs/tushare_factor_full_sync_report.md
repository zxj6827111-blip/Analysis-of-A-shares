# Tushare adj_factor 全量同步报告

| 项目 | 值 |
|---|---|
| 日期 | 2026-07-26 16:41 ~ 16:56(+0800) |
| 判定 | **PASS — TUSHARE_FACTOR_DATASET_READY=true** |
| sync_run_id | `tsfactor_20260726T164136_106c72e4` |
| **factor dataset_id** | **`tushare_adjfactor_1d_20260726_acc8d3cadc79`**(ready) |
| source/adjustment/dataset_type | tushare / adj_factor / **factor** |
| 命令 | `python scripts/sync_market_data.py --source tushare --adjustment adj_factor --mode full --universe-file <冻结宇宙> --rate-per-min 400 --coverage-out tmp/tushare_factor_symbol_coverage.csv`(前置 preflight/dry-run rc=0 零写入) |

## Token 安全

`token_configured=true`(经 `ts.get_token()` 读取);全程未打印/记录/持久化 Token 任何信息;manifest `token_exposed=false`;静态守卫测试禁止字面量 `set_token(` 与 Token 打印。

## 同步统计(实测)

| 指标 | 值 |
|---|---|
| 候选宇宙 | 5796(冻结 universe CSV,sha `fef78967…`) |
| **factor_ready** | **5554** |
| no_factor | 242(全部为最新年缺席的北交所证券——Tushare 无其历史因子,分类 `no_factor_expected`;**329 只在市北交所股票有因子**) |
| provider_failed / quality_failed | **0 / 0** |
| 因子记录总数 | **16,545,201** |
| API 调用 | 5796(=1 次/股)+2 次 stock_basic;**限频 0 次、重试 0 次** |
| 速率 | 386.3 次/分(上限 400) |
| 耗时 | 900.2s(15 分钟) |
| 原始缓存 | `E:\AStockData\factors\tushare\adj_factor\tsfactor_20260726T164136_106c72e4\*.csv`(5554 个,按 sync_run 版本化) |

## 过程能力(全部实测/复用 Gate A 机制)

限速(逐调用间隔)、指数退避(Provider `_call_with_retry`)、每 25 只原子 checkpoint、`--resume` 同 sync_run_id 续传、Windows 排他锁(`(root,tushare,adj_factor,1d)` 作用域)、逐股成功/失败记录、无 warning 静默跳过、不覆盖旧 ready、全部完成后原子发布。

## 覆盖分类(tmp/tushare_factor_symbol_coverage.csv,5796 行)

- factor_ready 5554(included);no_factor_expected 242(北交所缺席股,excluded);mapping_failed/provider_failed/excluded_non_equity/raw_missing 均 0。
- stock_basic(L+D) 元数据关联:list_status/list_date/delist_date 全量记录;2026 年退市股(如 000004 国华退/002808 恒久退/002898 赛隆退)有完整因子且含停牌期间公司行为。

## 派生信号宇宙定义

`local_vendor raw 可用 ∩ Tushare factor 可用 ∩ 有效A股或北交所` = **5554 只**;242 只缺因子股票在派生层显式排除并记录原因,回测选择 dataset 外股票将硬失败,不会以 raw 价格顶替 qfq。
