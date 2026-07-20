# A 股前台回测展示系统 — 实施计划

| 项目 | 说明 |
|------|------|
| 文档状态 | 正式计划（第二阶段产品化） |
| 关联文档 | `A股多周期TN6指标与八卦回测实施计划.md`、`GROK_IMPLEMENTATION_REPORT.md` |
| 中心代码 | `wtpy/apps/astock/` |
| 日期 | 2026-07-20 |

---

## 1. 背景与目标

本仓库在 WonderTrader / wtpy 之上，已具备较完整的 **A 股研究与组合回测扩展**（通达信式公式、多周期信号、八卦标注、纯 Python 组合回测、CLI 与报告）。既有计划明确 **第一版不做 Web**；本文件定义 **第二阶段产品化**：为前台提供可交互的展示与回测配置能力。

### 1.1 用户目标

1. **前台展示界面**：本地可启动的 Web 页面，用于规则选择、回测配置与结果查看。
2. **单一规则回测**：从内置（及自定义）规则库中选择 **一条** 规则跑回测。
3. **多规则叠加回测**：同时选择 **多条** 规则，按组合逻辑（至少 AND / OR）叠加后回测。
4. **买卖规定可配置**：例如信号发出后 **T+1 买入、T+2 卖出**（交易日语义，见第 4 节）。
5. **规则自定义**：前台输入公式后自动校验并保存到系统，进入可选规则列表。

### 1.2 非目标（本阶段不做或后置）

- 不改造 WonderTrader C++ 回测内核作为 A 股主路径。
- 不把现有 `wtpy/monitor` 策略部署模型直接当成 A 股 TN6 回测 UI（可共用技术栈，语义分离）。
- 第一版不做复杂规则图编排（权重、顺序门控、多层分组）— 仅 `all` / `any`。
- 第一版不强制实现「卖出信号公式」出场（P2）。
- 全市场超长区间性能优化可作为加固项，不阻塞 MVP。

---

## 2. 现状基线

| 能力 | 现状 | 主要入口 |
|------|------|----------|
| 指标/规则注册 | `IndicatorRegistry` + `storage/astock/indicators/registry.json` | CLI `import-indicator` / `list-indicators` |
| 公式编译运行 | parser / compiler / runtime（`XG` → 布尔信号） | `wtpy/apps/astock/indicators/` |
| 单/多指标信号 | 多次 `--indicator` + `--combine all\|any` | `study.py`、`cli.py` |
| 组合回测 | 纯 Python `PortfolioBacktester` | `strategy.py` |
| 买卖节奏 | 信号收盘后 → **下一交易日开盘买入**；`hold=N` 持有后开盘卖；可选止损/止盈（触发后 T+1 开盘） | CLI `--hold` / `--stop-loss` / `--take-profit` |
| 报告 | `outputs/astock/<run_id>/`（CSV/JSON/XLSX） | `reports.py` |
| A 股专用前台 / HTTP API | **无** | — |
| 监控 Web | FastAPI + 静态页，面向原生策略回测产物 | `wtpy/monitor/` |

结论：**业务内核基本齐**；缺口是服务层抽离、执行参数产品化（可配置 T+N）、规则 CRUD 产品模型、HTTP API 与前台。

---

## 3. 产品对象模型

避免把「规则」和「一次回测配置」混为一谈。

```text
Rule（规则 / 指标）
  = 公式 + 元数据 + 编译状态 + 是否内置 + 可回测/formal 标记

ExecutionPolicy（买卖规定）
  = 入场延迟 / 持有或出场约定 / 周期 / 风控 / 费用覆盖（可选）

BacktestJob（回测任务请求）
  = 规则集 + 组合方式 + ExecutionPolicy + 股票池/区间 + 研究|正式模式

BacktestRun（一次运行结果）
  = run_id + 状态 + 指标摘要 + 产物路径 + 可复现 meta
```

CLI 今日将规则与执行参数揉在 `backtest` 命令中；前台与 API 应拆开展示，便于规则库复用与常用买卖规定保存。

---

## 4. 买卖规定（T+N）语义

### 4.1 现有引擎语义（测试已固化，须兼容）

- 信号在 **当日 K 线收盘后** 成立。
- **买入（默认）**：下一交易日 **开盘**（相对信号日为 **T+1 交易日**）。
- **卖出**：`hold=N` 表示持有 N 个 **周期会话** 后，在下一可卖日 **开盘** 卖出。
  - DAY 下 `hold=1`：买入后下一交易日开盘卖（常见「短持」）。
- 持仓期间重复信号 **不重置** 剩余持有期。
- 涨停等导致无法买入则跳过；跌停/停牌推迟卖出。
- 止损/止盈按价格触发后，**下一交易日开盘** 执行。

### 4.2 前台第一版暴露参数

| 参数 | 含义 | 与引擎映射 |
|------|------|------------|
| `period` | `DAY` / `WEEK` / `MONTH` / `DWM` | 已有 |
| `entry_lag_sessions` | 信号后第几个交易日（或周期约定）买入，默认 `1` | **需扩展**：当前写死 next open |
| `hold` | 持有期数 N | 已有 |
| `stop_loss` / `take_profit` | 比例（0–1） | 已有 |
| `combine` | `all` / `any` | 已有（多规则时） |

### 4.3 示例：「信号后 T+1 买、T+2 卖」（DAY）

推荐映射：

- `entry_lag_sessions = 1` → 信号日后第 1 个交易日开盘买入。
- `hold = 1` → 买入后下一交易日开盘卖出（相对信号日为第 2 个交易日开盘卖，与现有 `test_day_hold1_t1_next_open_sell` 一致：信号 D1 → 买 D2 开 → 卖 D3 开）。

前台必须用 **中文说明 + 时间轴示意**（信号日 / 买入日 / 预计卖出日），明确为 **交易日** 而非自然日（默认决策：一律交易日）。

### 4.4 引擎改造优先级

| 项 | 优先级 | 说明 |
|----|--------|------|
| 可配置 `entry_lag`（≥1） | P0 | UI「T+1 / T+2 买」依赖 |
| UI 文案「T+k 卖」与 `hold` 双向换算 | P0 | 产品层 + 单测 |
| `exit_lag_from_signal`（自信号日起固定第 N 日卖） | P1 | 与 hold 语义冲突时再实现 |
| 卖出信号公式（另一条 XG 作出场） | P2 | 需求未强制 |

CLI 同步增加 `--entry-lag`，默认 `1`，保持旧行为兼容；`run_meta` 写入完整 ExecutionPolicy。

---

## 5. 规则库与自定义规则

### 5.1 内置规则

- 真源：`storage/astock/indicators/registry.json`（可扩展字段，如 `builtin` / `source`）。
- 列表展示：`id`、名称、简介、`compile_status`、是否可回测、`formal_backtest_allowed`、适用周期。
- 不可回测项（如 `source_required`）灰色展示；禁止正式回测；研究模式按既有 flag 放行并强提示。

### 5.2 自定义规则流程

```text
用户输入 name + formula_text（通达信风格，需含 XG: 或等价选股输出）
  → API 校验：parser / compile
  → 失败：返回错误信息（尽量含定位）
  → 成功：
       - 写入 registry（建议 id：user_<slug>_<hash8>）
       - 公式落盘：storage/astock/indicators/user/<id>.txt
       - source=user，formal_backtest_allowed 默认 false
  → 可选：二次「确认源码」升 formal（复用既有 confirm / provenance 流程）
```

质量与安全：

- 公式长度上限；运行时保持无外部 I/O 沙箱。
- 重名：确认覆盖或自动版本号。
- 删除：软删除（`archived`），避免历史 run 断链。
- 审计：创建时间、公式 hash、编译结果写入注册表。

### 5.3 多规则叠加

第一版对齐 CLI：

- `rule_ids[]` + `combine: all | any`。
- 八卦（`--with-bagua`）仅旁路标注，**不参与** 布尔 AND/OR（与既有计划一致）。

第二版可考虑权重、顺序过滤、分组 — **不进 MVP**。

---

## 6. 目标架构

采用 **薄 API + 复用现有引擎 + 独立前台**。

```text
┌─────────────────────────────────────────────┐
│  Frontend（Vue3/Vite 或等价 SPA）            │
│  规则库 | 回测工作台 | 任务进度 | 结果图表  │
└───────────────────┬─────────────────────────┘
                    │ HTTP/JSON
┌───────────────────▼─────────────────────────┐
│  AStock Web API（FastAPI）                   │
│  /api/v1/rules | policies | backtests | …  │
└───────────────────┬─────────────────────────┘
                    │ 同步 / 线程池任务
┌───────────────────▼─────────────────────────┐
│  Service 层（从 cli.py 抽离）                │
│  RuleService | SignalService | BacktestSvc  │
│  RunsService | JobsService                  │
└───────────────────┬─────────────────────────┘
                    │
┌───────────────────▼─────────────────────────┐
│  既有内核                                     │
│  registry / formula / study                  │
│  PortfolioBacktester / reports / storage     │
└─────────────────────────────────────────────┘
```

与 `WtMonSvr` 关系：可同机不同端口，或同进程挂载 `/astock/*`；**API 与页面使用独立命名空间**，不混用原生策略 BT 语义。

---

## 7. 分阶段实施

### 阶段 0：契约与原型（约 0.5–1 人日）

- 冻结：T+N 日历示意、字段表、错误码约定。
- 技术选型：API 使用项目已依赖的 **FastAPI**；前端推荐 **Vue3 + Vite**，构建产物放入 `wtpy/apps/astock/web/static/` 或仓库 `frontend/astock/`。
- 产出 REST 契约（可先表格，后 OpenAPI）。

**交付：** 本文档已确认 + 接口字段草表。

---

### 阶段 1：服务层抽离（约 1–2 人日）

从 `cli.py` 抽出可测 Python API；Web **优先调用 service**，不依赖 `subprocess` 调 CLI（CLI 改为薄封装）。

| 建议模块 | 职责 |
|----------|------|
| `service/rules.py` | list / get / create / update / archive / validate |
| `service/signals.py` | 生成 `SignalEvent` |
| `service/backtest.py` | 审计、回测、写报告 |
| `service/runs.py` | run 索引与产物读取 |
| `service/jobs.py` | 后台任务（内存线程池 MVP） |

**交付：** 现有 pytest 仍绿；脚本可直接 `service` 跑通小池回测。

---

### 阶段 2：执行引擎 T+N 参数化（约 1–2 人日）

- `PortfolioBacktester.run(..., entry_lag=1, ...)`。
- 扩展 `tests/apps/astock/test_backtest_engine.py` 等。
- CLI `--entry-lag`（默认 1）。
- `run_meta` 完整记录 ExecutionPolicy。

**交付：** 「信号 → T+1 买 → hold=1 → T+2 卖」有测试与 meta。

---

### 阶段 3：HTTP API（约 1.5–2 人日）

示例入口：`wtpy/apps/astock/api.py`，启动：`python -m wtpy.apps.astock serve --port 8765`。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/health` | 存储、registry 可读性 |
| GET | `/api/v1/rules` | 规则列表（builtin + user） |
| GET | `/api/v1/rules/{id}` | 详情（含 formula） |
| POST | `/api/v1/rules` | 创建自定义规则 |
| POST | `/api/v1/rules/validate` | 只编译不保存 |
| PATCH | `/api/v1/rules/{id}` | 更新用户规则 |
| DELETE | `/api/v1/rules/{id}` | 软删用户规则 |
| GET | `/api/v1/universe/summary` | 股票池规模、区间提示 |
| POST | `/api/v1/backtests` | 提交回测任务 |
| GET | `/api/v1/backtests/{run_id}` | 状态 + metrics 摘要 |
| GET | `/api/v1/backtests/{run_id}/artifacts/{name}` | equity / fills / signals 等 |

任务模型：

- 小池可同步；长任务 **异步**（`queued` → `running` → `succeeded` | `failed`）。
- 默认并发 1～2，防止全市场内存打爆。

**交付：** curl/httpx：建规则 → 回测 → 拉取 equity。

---

### 阶段 4：前台页面（约 3–5 人日）

#### 4.1 规则库

- 表格：内置 / 自定义筛选、可回测标记。
- 新建/编辑：公式编辑器 + 校验 + 保存。

#### 4.2 回测工作台（核心）

- 规则单选/多选 + `combine`。
- 买卖规定表单（`entry_lag`、`hold`、止损止盈、周期）。
- 时间轴预览：信号日 → 买入日 → 预计卖出日。
- 股票池：预设小池 / 粘贴代码（全市场二次确认，视决策）。
- 日期区间；研究模式开关与醒目警告。
- 提交回测与进度展示。

#### 4.3 结果页

- KPI（收益、回撤、胜率、交易次数等，对齐现有 metrics）。
- 权益曲线、成交表、信号表。
- 下载 CSV/XLSX、复制 `run_id`。

#### 4.4 历史任务

- 扫描 `outputs/astock/*/run_meta.json` 和/或维护 `runs_index.json`。

技术建议：图表 ECharts；大表注意性能；长任务轮询 1–2s。

**交付：** 本地完成「内置规则 + T+1/hold1 小池回测看曲线」与「新建公式 → 列表可见 → 再回测」。

---

### 阶段 5：加固与文档（约 1 人日）

- API / 关键路径回归测试。
- 启动与使用说明（随代码或用户要求再补独立操作手册）。
- UI 默认小池；全市场二次确认。
- 不得绕过 formal / research 审计规则。

---

## 8. 数据与存储约定

| 数据 | 路径建议 |
|------|----------|
| 规则注册表 | `storage/astock/indicators/registry.json` |
| 用户公式源文件 | `storage/astock/indicators/user/<id>.txt` |
| 回测输出 | `outputs/astock/<run_id>/` |
| 任务索引（可选） | `outputs/astock/runs_index.json` |
| ExecutionPolicy 预设（P1） | `storage/astock/policies/*.json` |

每次运行的 `run_meta.json` **必须** 可复现：`rule_ids`、`combine`、`entry_lag`、`hold`、`period`、股票池、起止日、审计 flag、公式 hash 等。

---

## 9. REST 请求体草表（Backtest）

`POST /api/v1/backtests` 建议字段：

```json
{
  "rule_ids": ["735_xxx"],
  "combine": "all",
  "period": "DAY",
  "entry_lag_sessions": 1,
  "hold": 1,
  "stop_loss": null,
  "take_profit": null,
  "codes": ["sh600000", "sz000001"],
  "start": "20200101",
  "end": "20241231",
  "with_bagua": false,
  "dwm": false,
  "research_unconfirmed_formula": false,
  "research_unadjusted": false,
  "run_id": null
}
```

`POST /api/v1/rules` 建议字段：

```json
{
  "name": "我的选股",
  "formula_text": "MA5:MA(C,5);\nXG:C>MA5;",
  "description": "可选说明",
  "periods": ["DAY"]
}
```

正式 OpenAPI 在阶段 0/3 落地时再生成或手写 YAML。

---

## 10. 待确认决策（实施前收紧范围）

| # | 议题 | 建议默认 |
|---|------|----------|
| 1 | T+N 使用交易日还是自然日 | **交易日** |
| 2 | 自定义规则默认是否允许 formal 回测 | **否**（仅研究；确认后可升 formal） |
| 3 | 第一版股票池范围 | **预设小池 + 粘贴列表**；全市场二次确认或二期 |
| 4 | 前端技术 | **Vue3 + Vite**；若极简可用服务端渲染，体验较弱 |
| 5 | 与 monitor 同进程还是独立端口 | **独立端口**更清晰；也可挂载 `/astock/*` |

决策变更时更新本文档对应章节，并同步测试与 `run_meta` 字段。

---

## 11. 里程碑与验收

| 里程碑 | 验收标准 |
|--------|----------|
| M1 服务层 | CLI 行为不变；service 可脚本调用 |
| M2 T+N | 测试覆盖 `entry_lag` + `hold`；与「T+1 买 / T+2 卖」说明一致 |
| M3 API | 自定义规则 CRUD + 异步/同步回测 + 拉取结果 |
| M4 UI | 单规则、多规则 AND/OR、买卖规定、自定义规则、结果展示贯通 |
| M5 加固 | 关键回归；小池端到端可复现 |

---

## 12. 推荐实施顺序

```text
① 确认第 10 节决策（尤其 1–3）
② 服务层抽离（阶段 1）
③ 引擎 entry_lag + 测试（阶段 2）
④ FastAPI（阶段 3）
⑤ 回测工作台 + 规则库 UI（阶段 4）
⑥ 结果可视化与历史任务（阶段 4 后半）
⑦ 审计 / 性能 / 文档（阶段 5）
```

---

## 13. 工作量粗估（单人、熟悉本仓库）

| 阶段 | 粗估 |
|------|------|
| 0 契约 | 0.5–1 人日 |
| 1 服务层 | 1–2 |
| 2 T+N 引擎 | 1–2 |
| 3 API | 1.5–2 |
| 4 前台 | 3–5 |
| 5 加固 | 1 |
| **合计** | **约 8–13 人日** |

不含：全市场性能专项、卖出信号公式、复杂规则编排。

---

## 14. 与既有计划的关系

| 文档 | 关系 |
|------|------|
| `A股多周期TN6指标与八卦回测实施计划.md` | 第一阶段：数据、公式、回测内核、CLI；**明确不做 Web** |
| **本文档** | 第二阶段：在内核可用前提下做前台、API、规则产品库与 T+N 产品化 |
| `GROK_IMPLEMENTATION_REPORT.md` | 实现状态与 formal/research 证据，实施时不得削弱审计约束 |

本阶段 **不推翻** formal/research、公式 provenance、T+1 持仓与成本模型，只做配置可视化、规则库、执行参数产品化与结果可读化。

---

## 15. 关键代码索引

| 路径 | 用途 |
|------|------|
| `wtpy/apps/astock/cli.py` | CLI 入口；待薄封装 |
| `wtpy/apps/astock/strategy.py` | `PortfolioBacktester` |
| `wtpy/apps/astock/study.py` | 信号计算与 `combine_signals` |
| `wtpy/apps/astock/indicators/` | 公式与 registry |
| `wtpy/apps/astock/config.py` | `AStockConfig` / 成本 |
| `wtpy/apps/astock/reports.py` | 报告写出 |
| `storage/astock/indicators/` | 注册表与映射 |
| `outputs/astock/` | 回测产物 |
| `tests/apps/astock/` | 行为契约（尤其 `test_backtest_engine.py`、`test_risk_t1.py`） |
| `wtpy/monitor/` | 仅参考 FastAPI/静态资源模式，不直接复用业务 |

---

## 16. 修订记录

| 日期 | 说明 |
|------|------|
| 2026-07-20 | 初版：前台回测展示、单/多规则、T+N 买卖规定、自定义规则与分阶段实施计划落库 |


---

## 17. 落地状态（2026-07-20）

已实现并接入仓库：

| 项 | 状态 | 说明 |
|----|------|------|
| 服务层 | 已完成 | `wtpy/apps/astock/service/`（rules / backtest / runs / jobs） |
| entry_lag (T+N 买) | 已完成 | `PortfolioBacktester.run(entry_lag=…)` + CLI `--entry-lag` |
| HTTP API | 已完成 | `wtpy/apps/astock/api.py`，`python -m wtpy.apps.astock serve` |
| 前台页面 | 已完成 | `wtpy/apps/astock/web/static/index.html` |
| 自定义规则 | 已完成 | 用户规则写入 `storage/astock/indicators/user/` + `user_registry.json` |
| 测试 | 已完成 | `tests/apps/astock` 全绿（含 entry_lag / rule_service / web_api） |

启动：

```text
python -m wtpy.apps.astock serve --host 127.0.0.1 --port 8765
```

浏览器打开 `http://127.0.0.1:8765/`。
