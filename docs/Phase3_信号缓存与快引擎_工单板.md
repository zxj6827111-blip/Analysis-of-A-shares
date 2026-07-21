# Phase 3 实施工单板 — 性能重构（三层缓存 + 双引擎）

**分支：** `feature/astock-research-phase1`  
**对应方案：** 升级方案 §七～八、§十九；任务拆分 §9  
**创建：** 2026-07-21  

---

## 目标（本迭代 MVP）

1. **信号层缓存**：同一指纹不重复算指标信号。  
2. **过滤层缓存**：同一信号缓存 + 星期/卦象规则 → 过滤结果可复用。  
3. **快速研究引擎**：逐信号路径统计（弱化资金竞争），用于网格初筛。  
4. **完整引擎**：仍走 `PortfolioBacktester`（默认）。  
5. **产物分级雏形**：`artifact_level=summary|full`，summary 少写大 Excel。  

```text
request
  → signal_cache (miss → compute → store)
  → filter_cache (miss → gua/weekday filter → store)
  → engine: fast | full
  → artifacts by level
```

---

## 状态

| ID | 内容 | 状态 |
|----|------|------|
| P3.0 | 工单板 | `[x]` |
| P3.1 | 拆分信号计算可调用 | `[~]` |
| P3.2 | signal_cache | `[~]` |
| P3.3 | filter_cache | `[~]` |
| P3.4 | fast_engine | `[~]` |
| P3.5 | 完整引擎保持默认 | `[ ]` |
| P3.6 | artifact_level | `[ ]` |
| P3.7 | Parquet 可选 | `[ ]` 本迭代可用 JSONL |
| P3.8 | 基准/测试 | `[ ]` |
| P3.E | commit push | `[ ]` |

---

## 模块

```text
wtpy/apps/astock/research/
  signal_cache.py
  filter_cache.py
  fast_engine.py
  executor.py      # 编排 cache + engine
  artifacts.py     # 分级写出
```

---

## 修订

| 版本 | 说明 |
|------|------|
| v0.1 | 开板开工 |
