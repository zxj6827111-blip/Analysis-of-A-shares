# Gate C 复验专项：D5 SQLite 迁移与写入可靠性（独立复验）

- 复验方式：隔离副本，全程不触碰生产库原件（`prod_db_untouched=true`）
- 证据：`tmp/gate_c_sqlite_revalidation.json`、脚本 `tmp/gate_c_revalidation/d5_revalidation.py`

## A. Fresh DB

- 空目录初始化 → `schema_meta.schema_version=3`，runs 41 列含全部 lineage 列 ✅
- 双源实验 run/metrics/experiment_variants 可写（variants_written=2），history API 可见 ✅

## B. Existing v2 DB（真实生产库只读副本还原 v2 态）

- 还原前记录 v3 列非空计数（execution_adjustment 7 / signal_factor_dataset_id 15 / formula_version 8 / raw 15）
- 迁移前后行数：runs 95/95、metrics 95/95、experiments 14/14、variants 68/68 —— **不减少** ✅
- 旧字段值不变化 ✅；新列存在 ✅；`schema_version=3` ✅；重复执行幂等 ✅
- 迁移后双源写入与 history 正常 ✅

## C. Partial-schema DB（版本 2 + 部分新列已存在）

- 自动补齐缺失列，无 duplicate column 错误 ✅
- 既有列值保留（`pre_existing_value_kept=true`）✅；终版 3；重复运行幂等 ✅

## D. 迁移失败回滚

- 注入迁移失败 → `SchemaMigrationError` 显式抛出（不静默）✅；版本戳保持 2（语义闸门回滚）✅
- **R1（P2，非阻断）**：加列 ALTER 因 python sqlite3 对 DDL 自动提交而不随事务回滚；幂等列守卫使干净重试收敛至 v3，无可观测破坏态。与整改报告"迁移完全事务化"的措辞存在 DDL 层面偏差，记录在案。
- 日志包含 old_schema_version / target_schema_version / migration_name / success|failure 四要素 ✅

## E. 写入失败语义

- 强制 SQLite 写失败 → `RunPersistenceError` 抛出（不吞）✅
- runs_index.json 该行 `status=failed` + error 记录 ✅；history 不伪造成功 ✅
- 惰性迁移会把 failed 行导入 SQLite（status=failed, error=sqlite_persist_failed），**从不显示为成功** ✅

## F. reconcile/backfill

- 隔离样本（2 个可验证 run + 1 个证据不全 run）：
  - dry-run：would_insert=2, failed=1 ✅
  - 正式：inserted=2，rv_orph_3 拒绝（不猜测 lineage）✅
  - 二次：inserted=0, already=2 —— **幂等、无重复** ✅
- runs/metrics/history 一致 ✅

## 结论

D5 全部硬性要求通过；R1 为 P2 非阻断发现。生产库此前已迁 v3 并补录 17 条（上一轮),本轮离线沙箱 fresh 库亦直接 v3（`tmp/gate_c_offline_revalidation.json` schema_version=3）。
