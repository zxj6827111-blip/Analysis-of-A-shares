# D5 — SQLite schema 迁移与写入可靠性整改报告

- 日期:2026-07-26;缺陷:Gate C P0-D5(db.py:21 版本未升 + runs.py:100-107 静默吞 upsert 异常 → 存量 v2 生产库永不迁移,新 run 全部不入 runs/metrics,history API 缺失,param_hash 去重失效)

## 1. Schema 版本

- 旧版本:**2**(runs 表 37 列,缺 signal_raw_dataset_id / signal_factor_dataset_id;无 signal_formula_version / execution_adjustment)
- 新版本:**3**(`wtpy/apps/astock/service/db.py: _SCHEMA_VERSION = 3`;runs 表 41 列)
- v3 新列:`signal_raw_dataset_id`、`signal_factor_dataset_id`、`signal_formula_version`(承载 formula_version)、`execution_adjustment`

## 2. 迁移机制(init_db)

- 链式注册表 `_MIGRATIONS = {1:(2,"v1_to_v2_multi_source_columns",…), 2:(3,"v2_to_v3_lineage_columns",…)}`,逐步迁移;
- 每步事务执行,异常回滚并抛 `SchemaMigrationError`(**不允许静默继续**;callers 传播,服务写入明确失败);
- 日志固定四要素:`old_schema_version / target_schema_version / migration_name / success|failure`(logger `astock.db`);
- 幂等:每列 ALTER 前用 `PRAGMA table_info` 守卫 → partial-schema 库不重复加列不报错;
- 无 schema_meta 行的库按**列存在性探测**版本(fresh→3 直建;legacy v1→链式 1→2→3),绝不误标最新;
- DB 版本高于代码 → 明确拒绝写入(SchemaMigrationError)。

## 3. 写入可靠性

- `upsert_run_from_index_row`:写入 41 列(新增 formula/execution_adjustment);事务失败 rollback+raise;
- `runs.append_run_index`:SQLite 失败时 (a) runs_index.json 该行 status→failed + error=`sqlite_persist_failed:…`(原状态存 original_status),(b) run_meta.json 同步标 failed+persist_error,(c) 抛 `RunPersistenceError` → 任务/variant 明确失败——**绝不"只写 JSON 当成功"**;
- backtest_artifacts.py / backtest.py(no_go 路径)/ experiments.py(runner 关联 upsert)三处 try/except-pass 全部移除;
- history 读取路径(list_runs)不再吞 DB 异常回退 JSON;delete_run 的 DB 删除不再吞异常;
- 一致性:runs/metrics/parameters/artifacts/experiments/experiment_variants 同一事务范式;detail API、history API、export、runs_index 的 lineage 同源一致(list 测试 + 在线实测)。

## 4. 三态真实验收(tmp/sqlite_migration_acceptance.json;生产库仅复制,原库未删未覆盖)

| Case | 基底 | 结果 |
|---|---|---|
| A fresh | 空目录 | 直接 v3,41 列,双源 lineage 写入+metrics+variants+history 全通过 |
| B 生产副本 | 真实生产库副本,忠实还原 v2(DROP 4 个全 NULL 新列+版本回 2,先核验 4 列在生产库中全为 NULL) | 迁移 v2→v3;71 runs/71 metrics/9 experiments/58 variants 全保留;最早行样本逐字段一致;重复 init_db 幂等;双源写入+history 通过 |
| C partial 副本 | 生产副本+预置 signal_raw_dataset_id/signal_formula_version、版本仍 2、预置值写入 | 幂等补齐其余列;`pre_existing_value` 保留;终版 3 |

说明:真实生产库已在修复后代码首次全量 pytest 时由 init_db 自动完成 v2→v3(纯 ALTER 增列,数据零变化;迁移前文件备份于 tmp/gate_c_remediation/db_backup/astock_experiments.pre_reconcile.sqlite3)。副本级验证按 B 案忠实还原后补做,与生产实际迁移路径逐条等价。

## 5. Reconcile 补录(scripts/reconcile_sqlite_runs.py → db.reconcile_runs_from_disk)

- 来源:runs_index.json 行 ∪ 含 run_meta.json 的 run 目录;仅写这两处实际存在的字段(**不猜测**);已在库 run 一律跳过(**不重复写**);支持 --dry-run 与 JSON 报告;
- 实测:dry-run 88/71/17;正式补录 **17 条、0 失败**;二次执行 **0 插入**(幂等);生产库 71→**88** runs;
- 补录样本核验:`bt_1785069570_2b5302`(Gate C B 档 internal)六字段 lineage 完整(source/adjustment/dataset/raw 父/factor 父/tsqfq_v1)。

## 6. 在线实证(修复后产品路径)

- 新实验 run(如 bt_1785075334_722020)实时入 runs+metrics+experiment_variants;history API 首屏可见;detail API `lineage` 与 SQLite 行、runs_index、run_meta 一致;
- 覆盖测试:tests/apps/astock/test_gate_c_remediation.py::TestD5*(fresh/v2/partial/版本/写入/异常不吞/history/reconcile 幂等)+ test_legacy_run_compatibility(版本钉更新为 3)。
