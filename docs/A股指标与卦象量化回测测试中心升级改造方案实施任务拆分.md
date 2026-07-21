# A股指标与卦象量化回测测试中心升级改造方案 — 实施任务拆分

**文档版本：** V1.0  
**编制日期：** 2026年7月21日  
**对应方案：** `docs/A股指标与卦象量化回测测试中心升级改造方案.md`（V1.0）  
**代码基线：** `wtpy/apps/astock`（分支以当前仓库为准）

---

## 1. 文档目的

将升级改造方案落实为**可排期、可验收、有依赖关系**的工单清单，便于按阶段推进，避免：

- 未封板正确性就扩大参数规模；
- 只改组合上限而不建设缓存与队列；
- 只按历史总收益排序、缺少样本外与稳健性评价。

**改造原则（与方案一致）：** 保留底层、重构实验层、强化计算层、完善评价层；不推倒重建现有回测引擎。

---

## 2. 一句话目标

将现有「固定买卖日程模板 × 有限网格 × Web 进程内线程批跑」的实验中心，升级为：

> **参数空间可任意组合 → 合法过滤 → 分层缓存 → 快/精双引擎 → 任务队列与 Worker → 样本外/稳健评价 → 长期研究计划** 的量化研究测试中心。

---

## 3. 现状与代码落点对照

| 能力 | 现状（主要落点） | 与方案差距 |
|------|------------------|------------|
| 单次回测 | `service/backtest.py`、`strategy.py`（`PortfolioBacktester`） | 信号 / 执行 / 报告耦合；无分层缓存 |
| 实验中心 | `service/experiments.py`：`WEEKDAY_TEMPLATES`（3）、`GUA_PRESETS`（3）、`DEFAULT_MAX_VARIANTS=50` / `HARD_MAX_VARIANTS=200` | 仍是模板轴，非独立参数轴 |
| 任务执行 | 实验：`ThreadPoolExecutor`；单任务：`service/jobs.py` 线程 Worker | 无独立消息队列、无重启续跑 |
| 存储 | `service/db.py` SQLite；`param_hash` 主要看请求参数 | 无 PostgreSQL；无完整研究指纹 |
| 日历 | `data/calendar.py`：`next_weekday_trading_day` 等 | 无节假日策略枚举；目标星期休市时易跳到「下一周同星期」 |
| 交易日程 | weekday 可覆盖 `entry_lag`/`hold`；买卖 open/close | 缺 planned/actual 成交日、`holiday_policy`、完整 `exit_reason` |
| 卦象 | `bagua/`、`service/gua.py`、过滤规则 | 需版本化与多参数轴扩展 |
| 前端 | `web/static/index.html` 实验中心（`expEstimate` / `expCreateAndStart` 等） | 固定模板 UI，非模块化参数空间 |
| 测试 | `tests/apps/astock/*`（weekday、entry_lag、session、gua、experiments 等） | 缺 holiday 策略、完整指纹、缓存、双引擎等 |

**结论：** Phase 1～2 以现有模块演进为主；Phase 3 起新建 `research/`；Phase 4 起基础设施（队列 / PG）升级。

---

## 4. 目标模块结构（方案 §三）

```text
wtpy/apps/astock/
├── research/                 # 新建（自 Phase 1 指纹起逐步填充）
│   ├── models.py             # 参数、实验、任务模型
│   ├── parameter_space.py    # 参数空间定义与展开
│   ├── constraints.py        # 非法组合过滤
│   ├── planner.py            # 实验规划与任务生成
│   ├── fingerprint.py        # 研究指纹
│   ├── signal_cache.py       # 指标信号缓存
│   ├── filter_cache.py       # 卦象及其他过滤结果缓存
│   ├── executor.py           # 单组实验执行
│   ├── optimizer.py          # 网格、随机、智能寻优
│   ├── validation.py         # 样本外与滚动验证
│   ├── scoring.py            # 综合评分与 Pareto
│   ├── regimes.py            # 牛熊震荡等划分
│   ├── tasks.py              # Celery 任务
│   └── reports.py            # 实验报告
├── service/experiments.py    # 编排入口（逐步薄封装 → research）
├── service/backtest.py       # 拆分信号 / 执行 / 报告
├── strategy.py               # 日程、退出、planned/actual
├── data/calendar.py          # 节假日策略
├── service/db.py             # 抽象 + 后期 PostgreSQL
└── web/static/index.html     # 参数空间 UI
```

---

## 5. 总体阶段与依赖

```text
P0 基线与范围锁定
  → P1 回测正确性封板          ★ 最高优先级，未完成不扩规模
    → P2 参数空间 MVP + 735×16 验收
      → P3 三层缓存 + 快/精双引擎 + 产物分级
        → P4 任务队列 + PostgreSQL + 续跑
          → P5 研究评价中心（样本外 / 稳健 / 热力图）
            → P6 自动寻优 + 持续研究计划
```

**现阶段明确不做：**

- 仅把 50/200 上限改成数十万；
- 未缓存、未分级产物就全量生成 Excel；
- 未样本外验证就按历史收益第一进入实盘；
- 第一阶段引入 Ray / 全面多机集群。

---

## 6. Phase 0 — 基线与范围锁定

**目标：** 不改核心逻辑前，锁定验收口径与回归基线。

| ID | 任务 | 产出 | 涉及 |
|----|------|------|------|
| P0.1 | 固化本实施任务拆分（本文档） | 阶段门禁、工单 ID | `docs/` |
| P0.2 | 735 标准 16 组手工核对表设计 | 信号日 / 买卖日 / 价 / 规则列 | 测试数据或表格 |
| P0.3 | 现有 `tests/apps/astock` 回归绿通 | 本地/CI 基线通过 | 测试套件 |

**依赖：** 无。  
**门禁：** 当前主路径单次回测与实验 MVP 行为可复现、测试不回退。

---

## 7. Phase 1 — 回测正确性封板

对应方案：**§十三、§十二、§二十一第一阶段、§二十二正确性验收**。

### 7.1 工单列表

| ID | 任务 | 说明 | 主要模块 |
|----|------|------|----------|
| P1.1 | 节假日顺延策略 | 实现至少：`next_trading_day`、`previous_trading_day`、`skip_trade`、`exact_weekday_only`；默认买卖日顺延至下一交易日 | `data/calendar.py`、`strategy.py` |
| P1.2 | planned / actual 成交日 | 每笔成交记录计划日、实际日、偏移天数、holiday 策略 | `strategy.py`、报告 / meta |
| P1.3 | 信号确认时点 vs 成交时点 | 收盘后确认的信号不得假设当日无滑点收盘成交；规则固化 + 测试 | `strategy.py`、`study.py` |
| P1.4 | 未来函数审查 | 周/月线未完结 bar、复权边界、指标计算可见性 | `study.py`、indicators runtime |
| P1.5 | 停牌 / 涨跌停 / 退市 | 对照方案补齐处理与用例 | `strategy.py`、`data/limit_rules.py` |
| P1.6 | 完整研究指纹 v0 | 参数 + 指标源码/包哈希 + 卦象规则版本 + 股票池 + 行情/复权/日历版本 + 引擎代码哈希 + 费率滑点等 | 新建 `research/fingerprint.py`；衔接 `db.param_hash` |
| P1.7 | 成本口径固定 | 手续费、印花税、滑点可配置、可写入 meta、可追溯 | CostConfig、`runs` meta |
| P1.8 | 735 人工核对案例 | 固定：周五收盘确认 735 → 下周一开盘买 → 周二～五开/收平 × 无卦象/最佳三爻 = 16 组 | 测试 + 可选 CLI |
| P1.9 | 退出原因枚举 | 至少：`stop_loss`、`take_profit`、`time_exit`、`weekday_exit`、`reverse_signal`、`gua_weakening`、`forced_exit`、`delisting_exit` | `strategy.compose_sell_reason` 等 |

### 7.2 建议迭代切片

| Sprint | 包含工单 |
|--------|----------|
| 1a | P1.1、P1.2 + 单测 |
| 1b | P1.3、P1.4、P1.5、P1.9 |
| 1c | P1.6、P1.7、P1.8 |

### 7.3 门禁

- 735 至少 2～4 组（最终 16 组）手工与引擎一致；
- 因果 / T+1 / session 相关既有测试全绿；
- 指纹变化后不得错误复用旧结果做去重命中。

---

## 8. Phase 2 — 参数空间 MVP

对应方案：**§四～§六、§二十一第二阶段**。

| ID | 任务 | 说明 | 主要模块 |
|----|------|------|----------|
| P2.1 | 参数轴数据模型 | 信号星期、买入偏移/星期、卖出偏移/星期、`buy_on`/`sell_on`、卦象多选、止损/止盈列表等 | `research/models.py`、`parameter_space.py` |
| P2.2 | 模板降级为快捷预设 | 保留现有三个 `WEEKDAY_TEMPLATES` 与卦象预设，但不再是唯一入口 | `service/experiments.py`、前端 |
| P2.3 | 笛卡尔展开 + 合法性过滤 | 时间逻辑非法、A 股 T+1、session 冲突、数据不足、用户排除 | `research/constraints.py`、`planner.py` |
| P2.4 | 组合预览 API | 理论组合数、过滤数、实际数、过滤原因、前 50 组预览、股票数、区间、搜索方式、预算提示 | `api.py`、experiments 编排 |
| P2.5 | 上限与预算绑定 | 重构 `DEFAULT_MAX_VARIANTS` / `HARD_MAX_VARIANTS`：研究模式可扩大，但必须绑定预算与预览，禁止无脑百万直跑 | experiments |
| P2.6 | 前端模块化实验配置 | 各参数轴独立控件 + 预设一键填充 | `web/static/index.html` |
| P2.7 | 735 自动 16 组 + 结果矩阵 | 4 卖出星期 × 2 时点 × 2 卦象；矩阵视图 + Excel | experiments、`reports.py` |
| P2.8 | 卦象多选轴 | 无 / 最佳三爻 / 偏多 / 自定义集合；规则版本字段 | 扩展 `GUA_PRESETS`、gua 服务 |

**依赖：** Phase 1 日程与指纹口径稳定。  
**门禁：** API/UI（或 CLI）一键跑通 735×16；非法组合可过滤且原因可见。

---

## 9. Phase 3 — 性能重构（三层缓存 + 双引擎）

对应方案：**§七、§八、§十九、§二十一第三阶段**。

| ID | 任务 | 说明 | 主要模块 |
|----|------|------|----------|
| P3.1 | 拆分 `backtest.run` | 信号计算 / 过滤 / 交易执行 / 报告可独立调用 | `service/backtest.py` |
| P3.2 | 指标信号缓存 | Key：行情版本 + 池 + 公式版本 + 参数 + 周期 + 区间 + 复权模式 | `research/signal_cache.py` |
| P3.3 | 过滤层缓存 | Key：原始信号缓存 ID + 信号星期 + 卦象规则版本 + 其它过滤 | `research/filter_cache.py` |
| P3.4 | 快速研究引擎 | 逐信号路径统计；弱化资金竞争与完整组合账户 | executor 快路径 / `study.event_path_stats` |
| P3.5 | 完整精确回测引擎 | 复用 `PortfolioBacktester`：资金、持仓上限、涨跌停等 | `strategy.py` |
| P3.6 | 产物三级保存 | 普通 Trial 仅摘要；候选 + 权益等；最终才完整 Excel/成交 | `reports.py`、experiments |
| P3.7 | Parquet 明细 | 信号 / 成交 / 权益曲线；库中存路径与哈希 | 存储约定 + IO |
| P3.8 | 性能基准 | 同一 16 组：无缓存 vs 有缓存耗时与重复计算次数 | 脚本或测试 |

**依赖：** Phase 2 参数空间；P1.6 指纹驱动缓存失效。  
**门禁：** 735 的 16 组共享同一批指标信号计算；磁盘不因大规模实验爆炸。

---

## 10. Phase 4 — 任务平台

对应方案：**§九、§十一、§二十一第四阶段、§二十二可靠性验收**。

| ID | 任务 | 说明 | 主要模块 |
|----|------|------|----------|
| P4.1 | 数据库访问抽象 | 本地开发可 SQLite；正式环境 PostgreSQL | `service/db.py` |
| P4.2 | 实验库表结构 | 含 experiments、trials、metrics、signal_cache、workers、schedules 等（对齐方案 §十一） | 迁移脚本 |
| P4.3 | Celery + Redis/RabbitMQ | 队列建议：`signal_queue`、`fast_backtest`、`full_backtest`、`validation_queue`、`report_queue`、`maintenance_queue` | `research/tasks.py` |
| P4.4 | Worker 生命周期 | 心跳、暂停、恢复、取消、超时、重试、幂等、失联重领 | 替换进程内 `ExperimentRunner` 主路径 |
| P4.5 | 进度与监控 API | 完成数、失败、队列长度、吞吐、失败原因 | `api.py` + 前端总览 |
| P4.6 | 部署草案 | Docker Compose：API + Redis + PG + Workers 等 | `deploy/` |

**依赖：** Phase 2/3 执行路径可被 Worker 调用。  
**门禁：** 服务重启后续跑；同一 Trial 不重复入库；可取消/重试。

---

## 11. Phase 5 — 研究评价中心

对应方案：**§十四～§十八、§二十一第五阶段、§二十二研究质量验收**。

| ID | 任务 | 说明 | 主要模块 |
|----|------|------|----------|
| P5.1 | 基础与风险指标扩展 | 年化、Sharpe、Sortino、Calmar、盈亏比、Profit Factor 等 | metrics / scoring |
| P5.2 | 横截面与时间切片 | 年度、分股票、行业（若有）、盈利票占比等 | metrics 表 + 报告 |
| P5.3 | 市场阶段划分 | 上涨 / 下跌 / 震荡 / 高低波动等 | `research/regimes.py` |
| P5.4 | 固定样本外 + Walk-Forward | 训练/验证/保留；滚动窗口汇总 | `research/validation.py` |
| P5.5 | 硬门槛 + 综合分 + Pareto | 样本外优先；多候选保留 | `research/scoring.py` |
| P5.6 | 结果六视图 | 总览、排名、热力图、卦象增益、年度/阶段矩阵、参数稳定区 | API + 前端 |
| P5.7 | 过拟合提示 | 孤立尖峰 vs 稳定平台 | scoring + UI |

**依赖：** Phase 4 能稳定存储大量 Trial 指标。  
**门禁：** 默认排序不唯总收益；输出样本外与稳定性信息。

---

## 12. Phase 6 — 自动寻优与持续运行

对应方案：**§十、§十六、§二十一第六阶段**。

| ID | 任务 | 说明 |
|----|------|------|
| P6.1 | 指标内部参数化 | 如 735：短/长均线、偏离阈值、均线上升条件等 |
| P6.2 | 网格 / 随机 / 分阶段搜索 | 大规模粗搜 + 优区细搜 |
| P6.3 | Optuna 接入 | Grid / Random / TPE / NSGA-II / Pruner；固定种子可复现 |
| P6.4 | 初筛 → 精确复测流水线 | 快引擎 Top% → 完整引擎 |
| P6.5 | Celery Beat 研究计划 | 日常 / 夜间 / 周末 / 月度 |
| P6.6 | 数据更新触发 + 漂移监测 | 行情更新后重跑候选、表现漂移告警 |
| P6.7 | 自动报告 + 模拟盘候选 | 研究报告；候选进入模拟观察流程 |

**依赖：** Phase 3～5。  
**门禁：** 有预算、提前停止与资源上限；不以单点最优直接上实盘。

---

## 13. 建议「下一迭代」8 个可交付工单（P1+P2 最小闭环）

面向**立刻开工**的最小闭环（正确性 + 参数空间 MVP），建议按序交付：

| 序号 | 工单 | 对应 ID | 交付物 |
|------|------|---------|--------|
| 1 | Calendar 节假日策略 + 单测 | P1.1 | 策略枚举、行为与旧逻辑差异说明、测试 |
| 2 | Fill 元数据：planned/actual + exit_reason | P1.2、P1.9 | 字段写入成交与导出 |
| 3 | 研究指纹 v0 + 与 param_hash 衔接 | P1.6 | `fingerprint` 模块 + 去重行为 |
| 4 | 735 核对用例（先 2～4 组，再扩 16） | P1.8 | 测试或对照脚本 |
| 5 | parameter_space + constraints + 预览 API | P2.1、P2.3、P2.4 | 后端 API |
| 6 | experiments 独立参数轴（模板仅预设） | P2.2、P2.5、P2.8 | 编排层改造 |
| 7 | 前端参数轴 UI + 16 组一键 | P2.6、P2.7（UI 部分） | 实验中心页面 |
| 8 | 结果矩阵视图 + Excel | P2.7 | 矩阵 + 导出 |

**实施提示：**

- 前端单文件体量大（`index.html`），可优先 **API/CLI 验收 16 组**，再改 UI；
- 指纹不全则禁止大规模信号缓存上线；
- 节假日策略变更后必须回归 `test_weekday_schedule`、`test_entry_lag`、`test_buy_sell_session` 等。

---

## 14. 735 标准验收案例（方案 §六，摘要）

**固定条件：**

- 周五收盘确认 735 信号；
- 下周一开盘买入；
- 不设止损、不设止盈；
- 卖出星期：周二、三、四、五；
- 卖出时点：开盘、收盘；
- 卦象：不加卦象、最佳三爻。

**组合数：** \(4 \times 2 \times 2 = 16\)。

**每组建议比较指标（Phase 2 可先子集，Phase 5 补全）：**

总收益、年化、最大回撤、Sharpe、Sortino、Calmar、胜率、盈亏比、Profit Factor、成交回合、交易次数、信号数量、卦象过滤后信号保留率、交易成本；后续补年度、市场阶段、样本外、邻域稳定性。

---

## 15. 分阶段验收门禁速查

| 阶段 | 功能 | 正确性 | 可靠性 | 研究质量 |
|------|------|--------|--------|----------|
| P1 | — | 无未来函数、T+1、session、节假日、涨跌停口径正确 | — | — |
| P2 | 任意参数轴 + 预览 + 735×16 | 合法过滤可见 | — | 矩阵可对比卦象增益雏形 |
| P3 | 缓存 + 双引擎 + 分级产物 | 指纹驱动缓存失效 | — | — |
| P4 | 队列、续跑、多 Worker | — | 重启续跑、幂等、取消重试 | — |
| P5 | 六视图、样本外、Pareto | — | — | 不唯历史收益第一 |
| P6 | 寻优 + 计划任务 | — | 长期运行、预算控制 | 漂移与报告 |

完整验收条目见方案 **§二十二**。

---

## 16. 风险与注意点

1. **星期锚定 vs 节假日：** 当前 `next_weekday_trading_day` 在目标星期非交易日时会继续寻找「之后的同一星期」，可能跨周；与方案默认「顺延至下一交易日」不一致，属 P1.1 必改点。  
2. **进程内线程：** `ExperimentRunner` 与 `JobStore` 不适合「服务器长期大规模跑」；P2 先做参数表达与正确性，规模与队列放在 P3～P4。  
3. **前端改动成本：** 优先稳定后端契约与 CLI/API 验收。  
4. **缓存正确性：** 完全依赖研究指纹；P1.6 未完成前不做正式信号缓存上线。  
5. **方案文档路径：** 主方案与任务拆分均在 `docs/` 目录；实施时以主方案条文为准，本文档管进度与工单。

---

## 17. 相关文档与代码索引

| 类型 | 路径 |
|------|------|
| 主方案 | `docs/A股指标与卦象量化回测测试中心升级改造方案.md` |
| 相关改造稿 | `docs/A股技术指标与卦象回测系统改造方案_v2.md`、`docs/A股技术指标与卦象回测系统整体改造方案.md` |
| 实验编排 | `wtpy/apps/astock/service/experiments.py` |
| 回测服务 | `wtpy/apps/astock/service/backtest.py` |
| 组合回测 | `wtpy/apps/astock/strategy.py` |
| 日历 | `wtpy/apps/astock/data/calendar.py` |
| 任务线程 | `wtpy/apps/astock/service/jobs.py` |
| SQLite | `wtpy/apps/astock/service/db.py` |
| Web | `wtpy/apps/astock/web/static/index.html` |
| 测试 | `tests/apps/astock/` |

---

## 18. 修订记录

| 版本 | 日期 | 说明 |
|------|------|------|
| V1.0 | 2026-07-21 | 初版：对照主方案与现有代码拆分 P0～P6 及 8 项近期工单 |

---

**下一步建议：** 从本文 §13 第 1 项（P1.1 节假日策略 + 单测）开工；或先完成 P0.2 的 735 手工核对表后再改引擎。
