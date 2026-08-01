# Gate C 最终验收失败项整改报告(总报告)

- 整改日期:2026-07-26;分支 `feat/multi-source-market-data`;HEAD `8ee03196aa20a4ce10f8674ead55dd34b33867e9`(未 commit、未 push)
- 数据资产:全部复用既有不可变数据集,零重新同步 —— 信号 A `tdxquant_front_1d_20260726_09b179b48611`(tdxquant/front)、信号 B `internal_tsfqfq_1d_20260717_c962acb8af26`(internal/tushare_factor_qfq,父因子集 `tushare_adjfactor_1d_20260726_acc8d3cadc79`)、L2 执行 `localvendor_none_1d_20260726_7089dc09c3c0`(local_vendor/none)
- 数据根:`E:\AStockData\datasets\market_data`(production/external)

## 判定

**READY_FOR_GATE_C_REVALIDATION = true**(本轮为开发整改,最终 READY_FOR_MULTI_SOURCE_PRODUCTION_BACKTEST 结论须由下一轮独立验收给出)
**READY_FOR_SURVIVORSHIP_SAFE_BACKTEST = false(维持)**

## 缺陷整改一览

| 缺陷 | 级别 | 修复 | 核心改动 | 专项报告 |
|---|---|---|---|---|
| D5 SQLite schema/写入 | P0 | ✅ | `_SCHEMA_VERSION` 2→3;链式幂等迁移(事务+回滚+start/success/failure 日志);upsert 不吞异常(RunPersistenceError + 持久化状态改 failed);reconcile 补录命令;runs 表新增 `signal_formula_version`/`execution_adjustment` | docs/sqlite_lineage_migration_report.md |
| D1 dataset 绑定 | P0 | ✅ | 新模块 `data/dataset_binding.py`:存在/ready/type/source/adjustment/period/角色/lineage/blob 抽检统一校验;错配→`DATASET_BINDING_MISMATCH` 等结构化 4xx;所有入口(sync/async/experiments)在创建前拦截 | docs/dataset_binding_validation_report.md |
| D2 双源实验配置化 | P0 | ✅ | 取消 `(tdxquant,front)+(tushare,qfq)` 硬编码;API 新增 `signal_variants` 列表;`dual_source_compare` 降级为正式模板别名 `(tdxquant,front)+(internal,tushare_factor_qfq)`;动态共同池+共同截止;variant 失败→实验 `failed` | docs/dual_signal_variant_configuration_report.md |
| D6 交易日历下限 | P1 | ✅ | repo 模式日历改由锁定执行数据集派生(20000403–20260717,6376 交易日,sha256 版本化+缓存);早于日历首日的信号显式排除不挤首日;日历 hash 入 L3 缓存键与 run meta | docs/historical_trading_calendar_fix_report.md |
| D7 北交所门禁 | P1 | ✅ | repo 模式复权/公司行为门禁改由锁定 adj_factor 因子数据集驱动(零 Baostock);BSE 43/83/87/920 canonical 解析修复(universe+repository+select_universe);共同池可正式跑 4962→5528 | docs/bse_repository_mode_fix_report.md |
| D3 缺数语义 | P2 | ✅ | 创建前逐股覆盖预检:not_in_dataset / no_data_allowlisted / blob 缺失(DATASET_CORRUPT)显式拒绝;区间无行情→errors 记 no_data_in_range;实验配置记录 requested/eligible/excluded+逐股原因 | 本报告 §D3 |
| D4 HTTP 错误语义 | P2 | ✅ | 不存在 dataset→404、非 ready→400、错配→400、symbol 不覆盖→400,统一 {code,message,dataset_id,requested_*,manifest_*,remediation};前端 api() 可读渲染 | docs/dataset_binding_validation_report.md |
| G1/G2/G3+负价 | P2 | ✅ | 实验结果页共同池横幅;任务详情"锁定数据集 lineage"完整块(10 字段+日历 hash);datastore 路由白名单修复;负价警告升级为表单+结果页可见块 | 本报告 §G |

## 验收证据(全部实测)

1. **默认 pytest**:`python -m pytest -q` → **734 passed / 0 failed / 0 skipped**(96.5s;基线 702 + 新增整改测试 32)。未用 --ignore。
2. **数据库三态**(tmp/sqlite_migration_acceptance.json,基于真实生产库副本,原库未删未覆盖):
   - fresh → 直接 v3,41 列,双源 lineage 写入+history 可查 ✓
   - 生产副本(忠实还原 v2:DROP 4 个全 NULL 新列+版本回 2)→ 迁移 v3,71 runs/71 metrics/9 experiments/58 variants 全保留、首行样本一致、重复迁移幂等 ✓
   - partial(生产副本+预置 2 列)→ 幂等补齐、预存值保留 ✓
   - 说明:真实生产库已由修复后 init_db 在修复后首次 pytest 中自动迁移 v2→v3(纯增列,71 行数据完好,备份于 tmp/gate_c_remediation/db_backup/);副本验证按上述忠实还原流程补做并通过。
3. **reconcile 补录**:dry-run 88 候选/71 已在库/17 待补;正式执行补录 17、失败 0;二次执行 0 插入(幂等);生产库现 88 runs,B 档 internal run 六字段 lineage 完整。
4. **单实验双 variant(在线,产品路径)**:
   - 小规模 `exp_3290cb2129`:33 只(6 北交所+5 负价股),2012→有效截止 20260717,v000(tdx)+v001(internal)均 succeeded;
   - 北交所专项 `exp_2a1d441663`:20 只 BSE,双 variant succeeded(287/89 与 107/107 信号/成交);
   - 正式 `exp_b0ef63c8c4`:500 只 2012–20260717,双 variant succeeded(93s);信号 43,457/47,020 与 Gate C 完全一致;成交 43,069/46,585(2016 前信号现可执行,D6 生效)。
   - SQLite runs/metrics/experiment_variants 全落库;history API 即时可见;detail API lineage 完整。
5. **错配/失败探针**(tmp/dataset_binding_error_probes.json):13/13 PASS —— 源错配×2、复权错配、不存在(404)、partial 信号集、partial 执行集、执行角色错配、执行源错配、信号股票不覆盖、白名单缺数股、执行股票不覆盖(服务层沙箱)、单 variant 失败→实验 failed、已废弃 (tushare,qfq) 对拒绝;全程 **0 个新 run**、无 500、无 fallback。
6. **离线复跑**(:8767,Provider/tqcenter/TdxDayReader/Tushare/Baostock/非回环 socket/ZIP 全部即调即抛,token 清除,沙箱存储不含 baostock adjustments 缓存):三实验(小/BSE20/500)双 variant 全 succeeded;**132 个 metrics 键逐字段 0 差异**;离线计数器全 0;沙箱 fresh SQLite v3 落库+history 可查。
7. **计数器(在线全程)**:provider(tdxquant/tushare/local_vendor)=0,tqcenter=0,TdxDayReader=0,**baostock=0**(上一轮在线尚有 1 次页面驱动外联,本轮彻底清零),非回环 socket=0。
8. **前端浏览器实证**:G1 共同池横幅(500/500、三重截止、逐 variant dataset+父集);G2 详情页"锁定数据集 lineage"10 字段+日历 sha;G3 task-detail→数据仓库正确落点 `view-datastore`;负价警告在表单选择与 tdxquant 结果页均为可见块(非 tooltip)。

## D3/D4 明细

- 不在 signal dataset:创建前 400 `SYMBOL_NOT_COVERED`(逐股原因列表,批量实验则预过滤并在 config.common_universe 记录 requested/common/excluded+reason);
- manifest 有记录但 blob 缺失:绑定期抽检 `DATASET_CORRUPT`(422),装载期 store 硬失败,不静默;
- 有 blob 但区间无行情:errors 显式 `no_data_in_range`,与"策略无信号"严格区分;
- 政策 allowlist 缺数股(如 SSE.STK.600193):`signal_no_data_allowlisted` 显式拒绝/排除,不再伪装零信号;
- 不存在 dataset:404 `DATASET_NOT_FOUND`(修复前 500);错误模型统一 8 字段,前端可读展示。

## G 项明细

- G1:实验结果页顶部动态横幅——请求/共同池数量、各源与执行集排除数、请求/数据集/有效三重截止、每个 variant 的 signal dataset(含 raw/factor 父集);
- G2:任务详情"回测设置"页新增完整 lineage 块(signal_data_source/adjustment/dataset_id、raw/factor 父集、formula_version、execution 三元组、ca_factor_dataset_id、交易日历来源+范围+sha);detail API 新增顶层 `lineage`;
- G3:`getCurrentModule()` 白名单补 `datastore`,任务详情路由下侧边导航到数据仓库正确落点;
- 负价:表单(回测+实验,双源勾选联动)与结果页可见警告块,明示"负价仅 L1 信号面,绝不进入 L2 成交/估值"。

## 遗留(非本轮范围,无新 P0)

- D8(P2,Gate C 已记录):fast 引擎有 CA 拦截时 run 状态口径("unsupported_corporate_action" 顶层标签 vs 完整 metrics)不变,variant 判定沿用 Gate C 语义;
- 幸存者偏差:维持 READY_FOR_SURVIVORSHIP_SAFE_BACKTEST=false,退市股 composite 属 Gate B;
- 双源结果差异归因结论(复权模型差异为主)不受本轮影响;本轮明确禁止也未做任何"让结果接近"的数据/信号修改。

## 交付物索引

docs/:gate_c_failure_remediation_report.md、sqlite_lineage_migration_report.md、dataset_binding_validation_report.md、dual_signal_variant_configuration_report.md、historical_trading_calendar_fix_report.md、bse_repository_mode_fix_report.md、gate_c_revalidation_readiness.md
tmp/:gate_c_failure_remediation.json、sqlite_migration_acceptance.json、dataset_binding_error_probes.json、dual_signal_single_experiment.json、historical_calendar_acceptance.json、bse_repository_acceptance.json、gate_c_remediation_commands.txt、gate_c_remediation_post.patch
镜像:E:\AStockData\reports
