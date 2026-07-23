# Phase 4 实施工单板 — 任务/试验平台（Queue + TrialStore）

**分支：** `feature/astock-research-phase1`  
**状态：** **accepted**（2026-07-21；gate 9 passed + API）  
**范围：** Phase 4 only — 不实现 Phase 5/6 评分 / Optuna。

---

## 目标

构建可单测（无 Redis/Postgres）的耐久任务与试验平台：

- 内存 / SQLite 队列后端，预留 Redis/PG 适配钩子
- 试验记录幂等写入（`idempotency_key` UNIQUE）
- Worker：claim → handler → ack / nack(retry)
- 平台门面：`ResearchPlatform`（供后续 API 使用）

---

## 完成清单

| ID | 内容 | 状态 |
|----|------|------|
| P4.1 | `queue_backend.py`：抽象 + Memory + Sqlite | `[x]` |
| P4.2 | `trial_store.py`：`research_trials` 幂等 | `[x]` |
| P4.3 | `worker.py`：`ResearchWorker.run_once` | `[x]` |
| P4.4 | `db_backend.py`：Sqlite + optional Postgres | `[x]` |
| P4.5 | `platform.py`：`ResearchPlatform` 门面 | `[x]` |
| P4.6 | `tests/.../test_phase4_gate.py` + API | `[x]` 9 passed |
| P4.7 | 导出 `research/__init__.py` | `[x]` |
| P4.E | 验收 / accepted | `[x]` accepted after gate green |

---

## 模块路径

```text
wtpy/apps/astock/research/queue_backend.py
wtpy/apps/astock/research/trial_store.py
wtpy/apps/astock/research/worker.py
wtpy/apps/astock/research/db_backend.py
wtpy/apps/astock/research/platform.py
```

默认库文件：`{storage_root}/research_platform.db`（任务表 + 试验表可同库）。

---

## 门禁用例（须全部通过）

1. enqueue + claim + ack 成功  
2. 幂等 trial insert：相同 `idempotency_key` → 一行  
3. queued 态 cancel 阻止执行  
4. nack/retry 递增 attempts，超 max 后 failed  
5. `reclaim_stale`：心跳过期 running → 可再 claim  
6. pause → resume 后可再 claim  
7. SqliteQueueBackend 关开后仍可 claim（耐久性）

```text
python -m pytest tests/apps/astock/test_phase4_gate.py -q --tb=short
```

---

## 明确不做（本 Phase）

- Redis 实现 / 生产集群  
- Optuna / 评分（Phase 5/6）  
- 对外 HTTP API：已挂 /api/v1/research/*  
- push 远程  

---

## 备注

- Claim：`UPDATE ... WHERE status='queued'`，单进程测试足够原子  
- Postgres：`PostgresDatabaseBackend.connect` 在无 `psycopg2` 时抛清晰 `ImportError`  


## Live stack note

Environment lacked Redis/Celery/Postgres; gate uses SQLite durable queue + TestClient fakes. Core claim/idempotency/reclaim proven in unit tests.
