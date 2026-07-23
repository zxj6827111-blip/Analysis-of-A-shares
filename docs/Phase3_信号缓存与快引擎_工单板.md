# Phase 3 实施工单板 — 性能重构（三层缓存 + 双引擎）

**分支：** `feature/astock-research-phase1`  
**状态：** **P3 收尾完成**（2026-07-21）

---

## 完成清单

| ID | 内容 | 状态 |
|----|------|------|
| P3.1 | 拆分信号计算 | `[x]` |
| P3.2 | 指标信号缓存 | `[x]` 磁盘 + 主路径 |
| P3.3 | 过滤层缓存 | `[x]` 卦象/八卦过滤接入 `use_signal_cache` |
| P3.4 | 快速研究引擎 | `[x]` |
| P3.5 | 完整引擎默认 | `[x]` 单次回测 full；实验 fast |
| P3.6 | 产物分级 | `[x]` summary/candidate/full |
| P3.7 | Parquet（可选） | `[x]` `parquet_io`；无 engine 时落 jsonl |
| P3.8 | 基准与测试 | `[x]` `scripts/bench_phase3_cache_fast.py` + 单测 |
| P3.9 | 执行层缓存 | `[x]` fast+summary 可命中 metrics |
| P3.10 | Top-N 精复测 | `[x]` `promote_top_n`（默认 3）实验完成后 full 复跑 |
| P3.E | commit/push | `[x]` `d3a15b8` |

## 默认行为

| 入口 | engine | artifact | cache | promote_top_n |
|------|--------|----------|-------|---------------|
| 单次回测 | full | full | off | — |
| 实验网格 | fast | summary | on | 3（按 total_return） |

## 缓存目录

```text
storage/astock/cache/signals/
storage/astock/cache/filtered_signals/
storage/astock/cache/execution/
```

## 验证

```text
pytest tests/apps/astock/ -q   # 244 passed
python scripts/bench_phase3_cache_fast.py
```
