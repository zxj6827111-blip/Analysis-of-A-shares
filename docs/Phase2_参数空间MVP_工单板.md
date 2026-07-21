# Phase 2 实施工单板 — 参数空间 MVP

**分支：** `feature/astock-research-phase1`  
**主方案：** `docs/A股指标与卦象量化回测测试中心升级改造方案.md` §四～六  
**更新日期：** 2026-07-21  

---

## 状态总览

| ID | 内容 | 状态 |
|----|------|------|
| P2.0 | 工单板 | `[x]` |
| P2.1 | 参数轴数据模型 | `[x]` `research/models.py` |
| P2.2 | 模板降级为预设 | `[x]` 自由轴时忽略 weekday_keys |
| P2.3 | 笛卡尔展开 + 合法性过滤 | `[x]` expand_axes + constraints |
| P2.4 | 组合预览 API | `[x]` POST `/api/v1/experiments/estimate` |
| P2.5 | 上限与预算绑定 | `[x]` soft 50 / hard 500 (API)；planner hard 2000 |
| P2.6 | 前端模块化实验配置 | `[x]` 模板/自由轴切换 + estimate 预览 + 735 一键 |
| P2.7 | 735×16 + 结果矩阵 | `[x]` preset + `matrix.build_result_matrix`；Excel 表可选未做 |
| P2.8 | 卦象多选轴 | `[x]` gua_keys 轴 |
| P2.T | 单测 + 全量回归 | `[x]` 聚焦 36 + **全量 225 passed** |
| P2.E | commit / push | `[x]` |

---

## 模块

```text
wtpy/apps/astock/research/
  models.py, parameter_space.py, constraints.py, planner.py, matrix.py
service/experiments.py + api.py  — free axes / legacy
```

## 预览示例

`POST /api/v1/experiments/estimate` 支持 `signal_weekdays_options` / `buy_options` / `sell_options` / `gua_keys` / `stop_loss_list` / `take_profit_list`，返回 theoretical / rejected / actual / preview / n / count。

## 修订

| 版本 | 说明 |
|------|------|
| v0.1 | 开板 |
| v0.2 | 三 Agent 完成核心；主会话合并冲突；225 passed；已推送 |
