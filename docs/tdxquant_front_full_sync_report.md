# TdxQuant 前复权日线正式全量同步报告(Gate C 第二阶段)

**判定:PASS — TDXQUANT_FRONT_DATASET_READY=true**

| 项目 | 值 |
|---|---|
| 日期 | 2026-07-26 |
| **dataset_id** | **`tdxquant_front_1d_20260726_09b179b48611`(ready)** |
| sync_run_id | `tdxfront_20260726T192205_e88ae49f` |
| source/adjustment/period | tdxquant / front / 1d(dataset_type=bars) |
| cutoff / anchor_date | 20260726 / **20260724**(最新交易日,front 锚) |
| tqcenter | 1.0.3(客户端在线,health=True) |
| 证据 | [tmp/tdxquant_front_full_sync.json](../tmp/tdxquant_front_full_sync.json)、[覆盖表](../tmp/tdxquant_front_symbol_coverage.csv)、控制台日志 tmp/_gd5_full_sync2_console.log |

## 执行结果

| 指标 | 值 |
|---|---|
| 候选宇宙 | 5796(冻结 vendor 并集 CSV,sha `fef78967…`,与既往轮一致) |
| 预排除 | 249(242 北交所旧码→920 迁移;7 最新年缺席退市,通用规则零硬编码) |
| **eligible** | **5547** |
| **imported** | **5528(99.66%)** |
| no_data | **19**——全部为 **2026 年退市股**(逐只证据:Tushare stock_basic=D + vendor 末交易日 2026-01~07),经显式 allowlist(--allow-no-data-file,19 行逐只 reason)按严格策略放行 |
| failed | **0** |
| **总K线** | **16,651,526** |
| 日期范围 | **19901219 ~ 20260724** |
| batch | 15(批→单只回退由 Provider 层完成:19 个含 Date=None 退市股的批自动拆单,655=370 批+285 单只) |
| API 调用 | 655 次 tq.get_market_data;重试 0 |
| 耗时 | **603s(约 10 分钟)** |
| 数据集体积 | **300.7 MB**(5528 个内容寻址 npz blob) |
| content_hash | manifest 记录;首跑(partial)与复跑行数一致(16,651,526)=确定性 |

## 严格 ready 过程(无粉饰)

1. **首跑**(sync_run `tdxfront_20260726T175922_a82e94de`):同参数完成 5528 ok / 19 no_data / 0 failed → **严格策略拒发 ready,发布 partial**(`no_data_not_allowlisted=19`),dataset `tdxquant_front_1d_20260726_0388f524869d` 保留在案;
2. **逐只取证**:19 只全部 Tushare stock_basic list_status=**D**,vendor 末交易日均在 2026 年(000004 国华退/002808 恒久退/002898 赛隆退/300029/300379 等)——与"TdxQuant 不提供退市数据"的实测结论一致(7 只退市探针全形态无数据);证据文件 tmp/_gd_nodata19_evidence.json + allowlist tmp/tdxquant_front_no_data_allowlist.csv;
3. **复跑**发布 ready(warning_symbol_count=19 记入 manifest.no_data_allowlist)。未在同步后将任何失败股改成 excluded;两个 dataset 均完整保留。

## 锁 / checkpoint / 中断(第八、九部分,真实演练)

- **双进程锁**:(root,tdxquant,front,1d) 排他;第二进程立即失败并回显 holder 元数据(pid/hostname/start_time/scope/root/sync_run_id),零 blob/manifest 写入;
- **强制中断**:kill 后零 manifest(无伪 ready)、checkpoint 存活、OS 自动释锁且 stale 元数据可读;
- **resume**:沿用原 sync_run_id;宇宙变更/根变更/版本不符均拒绝;**中断+续传总调用数 = 不中断基线(ceil(N/batch))→ 已完成股零重复调用**;
- **batch 抽样**:10/20/50 三档 content_hash 完全一致(结果与批大小无关);正式采用 15。

## 与既有数据集关系

旧 ready(local_vendor 执行集、tushare 因子集、internal 派生集)全部原样保留;本数据集仅新增。资产清单:执行 L2=`localvendor_none_1d_20260726_7089dc09c3c0`;信号 L1 现有两套:`internal_tsfqfq_1d_20260717_c962acb8af26` 与本集。
