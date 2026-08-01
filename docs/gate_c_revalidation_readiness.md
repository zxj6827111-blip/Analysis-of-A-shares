# Gate C 复验就绪声明

- 日期:2026-07-26;分支 `feat/multi-source-market-data`;HEAD `8ee03196aa20a4ce10f8674ead55dd34b33867e9`(整改改动均未 commit/push,见 tmp/gate_c_remediation_post.patch)

## 判定

# READY_FOR_GATE_C_REVALIDATION = **true**

依据(十九节硬门槛逐条):

| # | 硬门槛 | 结果 | 证据 |
|---|---|---|---|
| 1 | D5 存量库迁移通过 | ✓ | 生产副本忠实 v2 还原→v3 迁移,71 runs 全保留,幂等;fresh/partial 同过(tmp/sqlite_migration_acceptance.json) |
| 2 | SQLite 新 run 真实落库 | ✓ | 7 个在线新 run + 17 条 reconcile 补录全部在 runs/metrics 表(生产库 71→88→95,metrics 95,variants 68) |
| 3 | history API 可见 | ✓ | GET /api/v1/runs 首屏即新 run,lineage 字段齐 |
| 4 | D1 所有错配拒绝 | ✓ | 探针 13/13,结构化 4xx,0 新 run(tmp/dataset_binding_error_probes.json) |
| 5 | D2 单实验双 variant 可达 | ✓ | exp_3290cb2129 / exp_2a1d441663 / exp_b0ef63c8c4:同一实验两 variant 两 run,全 succeeded |
| 6 | D6 历史日历修复 | ✓ | 日历 20000403 起(6,376 日,sha 版本化);2012–2015 全引擎 1,496 笔成交、20160104 当日 0 笔 |
| 7 | D7 北交所 Repository 可用 | ✓ | 20 只 BSE 双源真实回测 succeeded;共同池可跑 5,528;Baostock=0 |
| 8 | D3 不再静默零信号 | ✓ | 覆盖预检 400/预过滤+逐股原因;no_data_in_range 显式;allowlist 股拒绝 |
| 9 | D4 不再返回 500 | ✓ | 不存在→404,其余 4xx 结构化,含 remediation;前端可读 |
| 10 | 页面追溯完整 | ✓ | G1 共同池横幅 / G2 lineage 10 字段+日历 sha / G3 datastore 路由,浏览器实证 |
| 11 | 在线与离线双 variant 均通过 | ✓ | 3 实验×2 variant×在线离线全 succeeded;132 metrics 键 0 差异 |
| 12 | Provider/TdxDayReader/Baostock/网络=0 | ✓ | 在线与离线计数器全 0 |
| 13 | 默认 pytest 通过 | ✓ | 734 passed / 0 failed / 0 skipped(基线 702+新增 32) |
| 14 | 无新 P0 | ✓ | 未发现;遗留仅 Gate C 已记录的 P2(D8 状态口径)与 Gate B 范畴(幸存者偏差) |

## 边界与声明

- 本轮为开发整改,**不自行宣布 READY_FOR_MULTI_SOURCE_PRODUCTION_BACKTEST=true** —— 该结论须由下一轮独立验收给出;
- **READY_FOR_SURVIVORSHIP_SAFE_BACKTEST = false(维持)**:退市股 composite(Gate B)未做,所有数据集幸存者偏差标注不变;
- 复验提示:D6/D7 使 L1 缓存键(factor_manifest_sha)与 L3 缓存键(calendar 三键)对旧缓存自然隔离,首次复验运行会重算信号(500 只约 40–50s/variant);生产库 v2→v3 迁移已发生(备份 tmp/gate_c_remediation/db_backup/),reconcile 幂等可重放;
- 未做任何"令双源结果接近"的数据或信号修改;两源结果差异仍由复权模型差异主导(Gate C 归因结论不变)。
