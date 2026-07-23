# Phase 1 实施工单板（回测正确性封板）

**分支：** `feature/astock-research-phase1`（基于最新 `origin/main`）  
**主方案：** `docs/A股指标与卦象量化回测测试中心升级改造方案.md`  
**任务拆分：** `docs/A股指标与卦象量化回测测试中心升级改造方案实施任务拆分.md`  
**更新日期：** 2026-07-21

---

## 1. 总原则

- Phase 1 **不扩**参数规模、不上 Celery/PG。
- 以「正确性 + 可复现 + 可验收用例」为门禁。
- 工作拆成 **4 条并行泳道 + 1 条汇总**，由主会话协调、子 agent 执行。

```text
        ┌──────────── Agent A ────────────┐
        │ 日历/成交元数据/退出原因/回归   │
        └───────────────┬─────────────────┘
                        │
main ──► phase1 分支 ───┼──► 汇总合并 / 全量相关测试
                        │
        ┌──────────── Agent B ────────────┐
        │ 研究指纹接入 service 层         │
        └─────────────────────────────────┘
        ┌──────────── Agent C ────────────┐
        │ 信号时点 / 未来函数审查与测试   │
        └─────────────────────────────────┘
        ┌──────────── Agent D ────────────┐
        │ 成本 meta + 735 核对用例扩展    │
        └─────────────────────────────────┘
```

---

## 2. 状态图例

| 标记 | 含义 |
|------|------|
| `[x]` | 已完成（含单测） |
| `[~]` | 进行中 / 部分完成 |
| `[ ]` | 未开始 |
| `[!]` | 阻塞 / 需人工确认 |

---

## 3. 工单明细

### 泳道 A — 日程与成交正确性（P1.1 / P1.2 / P1.9）

| ID | 内容 | 状态 | 模块 / 测试 |
|----|------|------|-------------|
| A1 | `holiday_policy`：next / prev / skip / exact | `[x]` | `data/calendar.py` |
| A2 | `resolve_weekday_session` planned/actual/shift | `[x]` | `calendar.py` |
| A3 | Fill：`planned_date` / `actual_date` / `shift_days` / `holiday_policy` | `[x]` | `strategy.py` |
| A4 | 退出原因：`time_exit` / `weekday_exit`（兼容 hold_expired） | `[x]` | `strategy.py` |
| A5 | 策略接入 `holiday_policy` 参数与 config 落盘 | `[x]` | `strategy.run` |
| A6 | 单测：holiday / 735 日程 smoke / hold 回归 | `[x]` | Agent A：相关 37 passed |
| A7 | 回归：`test_weekday_schedule` / entry_lag / session / risk | `[x]` | 汇总子集 52 passed（2026-07-21） |

**Agent A 目标：** 保证泳道 A 相关测试全绿；修因 reason 改名导致的断言；不扩大范围到指纹/Optuna。

---

### 泳道 B — 研究指纹 v0（P1.6）

| ID | 内容 | 状态 | 模块 / 测试 |
|----|------|------|-------------|
| B1 | `research/fingerprint.py` 三层指纹结构 | `[x]` | signal / filter / execution |
| B2 | `research_param_hash` 挂到 `service/db.py` | `[x]` | 与 `param_hash` 并存 |
| B3 | 回测结果 config / index 写入 `research_fingerprint` | `[x]` | Agent B：backtest summary/repro/index + db extra_json |
| B4 | 实验 variant 去重仍用 param_hash；FP 作元数据 | `[x]` | experiments 兼容旧去重 |
| B5 | 单测：参数/引擎变化使指纹变化 | `[x]` | + `test_research_fingerprint_wire.py` |
| B6 | 文档：param_hash vs research（工单/核对说明） | `[x]` | 本工单板 + 核对说明 |

**Agent B 目标：** 完成 B3～B4 最小接入；保持 `param_hash` 兼容；新增/扩展单测。

---

### 泳道 C — 信号时点与未来函数（P1.3 / P1.4）

| ID | 内容 | 状态 | 模块 / 测试 |
|----|------|------|-------------|
| C1 | 文档化：收盘确认不得当日无滑点收盘成交 | `[x]` | Agent C：审查结论 — 现逻辑已安全 |
| C2 | 审查：`buy_on=close` 且信号日=买入日是否允许 | `[x]` | entry_lag≥1，禁止同 bar |
| C3 | 周/月线未完结 bar 是否泄漏 | `[x]` | asof/include_open 默认安全 |
| C4 | 因果回归是否仍绿 | `[x]` | causal + 新测通过 |
| C5 | 新增/补强用例 | `[x]` | `test_signal_session_causality.py` |

**Agent C 目标：** 只读审查 + 必要最小修复 + 测试；发现大改动只出报告不擅自重构。

---

### 泳道 D — 成本口径与 735 验收（P1.7 / P1.8）

| ID | 内容 | 状态 | 模块 / 测试 |
|----|------|------|-------------|
| D1 | config/meta 固定写出 costs 全字段 | `[x]` | Agent D：summary/repro/run_meta/Excel |
| D2 | 零成本对照与 cost_impact 可追溯 | `[x]` | 既有 replay；成本字段已露出 |
| D3 | 735：周五信号→周一开买→周二～五开/收 子集 | `[x]` | `test_735_phase1_schedule.py` |
| D4 | 扩展：无卦 vs 过滤对照（service 层 mock） | `[x]` | `test_735_phase1_gua_contrast.py` |
| D5 | 核对表模板（人工/半自动） | `[x]` | `docs/Phase1_735核对说明.md` |

**Agent D 目标：** D1 检查补洞；D4 至少 2 组对照用例；D5 简短核对说明。

---

### 泳道 E — 汇总（主会话）

| ID | 内容 | 状态 |
|----|------|------|
| E1 | 合并各 agent 改动、解冲突 | `[x]` | 四泳道并行无文件冲突 |
| E2 | 跑 Phase1 关键 + **全量** `tests/apps/astock` | `[x]` | 关键 52 → **全量 206 passed**（2026-07-21） |
| E3 | 更新本工单板状态 | `[x]` | 本轮 |
| E4 | （用户确认后）提交 commit / 推远程 | `[ ]` | 待你确认 |

---

## 4. 并行约束（避免互相踩脚）

| 文件 | 主责 | 其他 agent |
|------|------|------------|
| `data/calendar.py` | A | 只读 |
| `strategy.py` | A（已改） | C 仅追加注释/最小校验；冲突找主会话 |
| `research/*` | B | 其他只读 |
| `service/db.py` | B | 其他只读 |
| `service/backtest.py` / `runs.py` | B | D 可读 costs 写出点 |
| `study.py` | C | 其他只读 |
| 新测试文件 | 各泳道自建，文件名勿冲突 | — |

---

## 5. 验收门禁（Phase 1 出口）

1. 节假日默认「顺延至下一交易日」与 planned/actual 可导出。  
2. 退出原因使用规范码（至少 time_exit / weekday_exit / stop_loss / take_profit）。  
3. research 指纹可计算且引擎代码变化会变。  
4. 735 日程子集自动化用例通过；有核对说明。  
5. 关键回归 + causal 测试通过；不破坏实验中心旧 API。  

---

## 6. 本轮 Agent 派单（执行中）

| Agent | 范围 | 产出 |
|-------|------|------|
| A | A6–A7 测试与修复 | 绿测列表 + 必要补丁 |
| B | B3–B6 | backtest/runs 接入 + 测试 |
| C | C1–C5 | 审查结论 + 测试/小修 |
| D | D1、D4、D5 | costs 补洞 + 735 扩展 + 核对说明 |

---

## 7. 本轮关键回归命令（汇总）

```text
python -m pytest tests/apps/astock/test_holiday_policy.py \
  tests/apps/astock/test_735_phase1_schedule.py \
  tests/apps/astock/test_735_phase1_gua_contrast.py \
  tests/apps/astock/test_research_fingerprint.py \
  tests/apps/astock/test_research_fingerprint_wire.py \
  tests/apps/astock/test_signal_session_causality.py \
  tests/apps/astock/test_weekday_schedule.py \
  tests/apps/astock/test_hold_close_exit.py \
  tests/apps/astock/test_entry_lag.py \
  tests/apps/astock/test_buy_sell_session.py \
  tests/apps/astock/test_causal_backtest_invariance.py \
  tests/apps/astock/test_risk_t1.py \
  tests/apps/astock/test_735_and_risk.py -q
# → 52 passed
```

---

## 8. 修订

| 版本 | 说明 |
|------|------|
| v0.1 | 初建工单板；代码侧 A/B 部分已在主会话落地 |
| v0.2 | 四 Agent 并行完成 A–D；汇总 52 passed；E4 待提交 |
| v0.3 | Agent 全量 `tests/apps/astock`：**206 passed**；修 e2e hold 断言 + tdx 实盘边界 |
