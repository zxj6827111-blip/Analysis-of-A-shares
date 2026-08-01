# TdxQuant 前复权全量同步 preflight 报告(Gate C 第二阶段)

| 项目 | 值 |
|---|---|
| 日期 | 2026-07-26 |
| 命令 | `python scripts/sync_market_data.py --source tdxquant --adjustment front --mode full --preflight --universe-file E:/AStockData/reports/vendor_full_import_universe.csv --batch-size 15` |
| 结果 | **rc=0,零写入(不写正式 blob、不发布 manifest)** |

## 1. preflight 输出(逐项)

| 检查项 | 值 |
|---|---|
| ASTOCK_ENV | production |
| Storage root(MARKET_DATA_ROOT) | `E:\AStockData\datasets\market_data`(存在) |
| tqcenter 文件 | `D:\通达信\PYPlugins\user\tqcenter.py` exists=True |
| tqcenter 版本 | **1.0.3**(与已知版本一致,文件头 docstring 解析) |
| **TdxQuant client health** | **True**(真实 InitConnect + 数据探针;客户端已登录) |
| Universe 文件 | 5796 行,eligible=5547,pre_excluded=249 |
| Universe sha256 | `fef7896754e0983b…`(与正式全量导入轮冻结值一致) |
| Universe hash(eligible) | `7e5efb629c159883…` |
| 锁文件 | (none)——无并发 building 任务 |
| checkpoint | (none) |
| batch/retry | 15;批失败→单只,2 次/只重试 |
| no_data 策略 | 首跑无 allowlist(严格);复跑挂 19 只证据 allowlist(见全量报告) |
| building/既有 tdxquant front 数据集 | (none)/(none)——不会覆盖旧 ready |
| 写权限 | True(root 探针写删) |
| 内存 | GlobalMemoryStatusEx 采集(psutil 缺省回退) |
| 磁盘 | 119.0 GB 空闲(≥5GB 门槛) |

## 2. dry-run 输出(rc=0,零客户端调用、零写入)

| 项 | 值 |
|---|---|
| source/adjustment/period | tdxquant / front / 1d |
| 候选 / eligible / 排除 | 5796 / **5547** / 249(`bse_legacy_code_migrated_to_920_segment`=242,`absent_latest_vendor_year_delisted_no_provider_data`=7) |
| 批次计划 | 370 批 × 15 |
| 预计调用 | ~370 批调用(DLL 层逐只 ≈5547 次拉取) |
| 预计耗时 | ~19 分钟(@0.2s/只实测) |
| cutoff | 20260726 |
| checkpoint 位置 | `<root>/sync_logs/checkpoint_tdxquant_front_1d.json` |
| 既有同类数据集 | (none),声明保留不覆盖 |

## 3. 客户端在线验证(第五部分要求,真实接口)

`tq.initialize(__file__)` 成功;单只/批量(含北交所 920 码)/1d/front/none/字段(Open/High/Low/Close/Volume/Amount 与 Provider 预期一致,另有 ForwardFactor 附加字段不入库)全部实测通过;交易日历 7 月 18 个交易日;`get_stock_list` 枚举不可用("server return none")→ 宇宙以 vendor 并集为准、逐股实测判定支持性。证据:`tmp/_gd1_client_verify.json`、`tmp/_gd1b_anchor_probe.json`、`tmp/_gd1c_universe_probe.json`。

## 4. 301107 基准(第六部分,vendor-native 真实验证)

目标周 2026-05-11~15 前复权:**O=20.15 H=20.25 L=19.15 C=19.65**。

- 周线全史拉取(period=1w):**逐值精确一致** ✅;
- 日线(锚定今日)本地聚合周线:**逐值精确一致** ✅(日线与周线口径互证);
- 记录在案的差异:当请求区间起点恰落在目标周内时,tqcenter 返回的周线 Open=19.72(H/L/C 仍正确)——DLL 区间构造怪癖,**正式流程一律全史拉取,不混用**;
- 正式 dataset 只含前复权**日线**;周线由下游 local_aggregate 聚合,vendor-native 周线仅作验证用途,不入 1d 数据集。
