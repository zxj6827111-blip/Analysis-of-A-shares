# PLAN-BAGUA-UX-V1.1 执行基线记录（M0）

记录时间：2026-09-11
执行者：本地 ZCODE
计划编号：PLAN-BAGUA-UX-V1.1

## 一、代码基线

- **开工 commit**：`89e31df`（fix(tests): CI 红转绿——公式依赖跳过标记、starlette 1.x 路由内省兼容、导入查重跨平台一致）
- **开工分支**：自 `feature/bagua-export-rule-picks`（与 origin 同步，工作区 clean）新开 `feature/bagua-workbench-ux`
- **工作区差异**：开工时 clean，无未提交修改需要保护
- **版本**：APP_VERSION v2.9.7（`wtpy/apps/astock/version.py`）
- **main 分支**：`89e31df` 与开工分支同源（feature/bagua-export-rule-picks 领先 main 若干提交）

## 二、开工时已知测试/CI 状态

- CI 在 v2.9.7（commit 89e31df）修复后全绿（详见记忆 astock-ci-green-v297）。
- 已知跳过：`tests/apps/astock/test_indicator_review.py` 中 13 个公式依赖测试在 CI 上因 `指标/` 目录被 .gitignore 而 skip（`requires_real_formulas` 标记）。本计划将两条公式（`735金叉及趋势.txt`、`先跌后涨新版5日外.txt`）收进测试 fixture 解除跳过（用户已确认公式可以进 git）。
- 已知依赖漂移风险：`requirements.txt` 中 `fastapi`、`uvicorn` 未钉版本。本计划钉住 fastapi/starlette（本地版本 fastapi 0.135.1 / starlette 0.52.1）。

## 三、旧 Excel 样本

旧导出样本路径（本地数据根 `E:\AStockData` 正式 L1/L2、cutoff 20260814）在验证阶段生成后归档至本目录 `samples/`。列结构以 `export_bagua_multi_period_xlsx`（`wtpy/apps/astock/service/bagua_query.py`）现有实现为准，本轮保证新旧兼容。

## 四、设计附件

- `bagua-workbench.source.html`：交互原型原始副本（未修改，来源 `C:\Users\zxj68\.codex\visualizations\2026\09\11\01a09095-4d28-7d12-a96b-1e3facc55654\bagua-workbench.html`）
- `bagua-workbench-preview.html`：补齐页面外壳（DOCTYPE/head/viewport/color-scheme）的独立预览，正文与原始副本逐字节一致
- 原型中的固定日期（2026/09/10 等）、四个示例标的、示例规则结果、模拟任务延时、Tweak 设计调节工具均**不进入**正式业务代码

## 五、关键既有实现锚点（实现前调研结论）

- 页面：`wtpy/apps/astock/web/static/index_v3.html`（13357 行），导航 `#mainNav` :1289，查询卦象视图 `#view-bagua-query` :1673
- 查询：`GET/POST /api/v1/bagua/query`、`POST /api/v1/bagua/batch/query`（`api_routes/bagua.py` :549-623；`service/bagua_query.py` `query_bagua` :1327）
- 导出：`POST/GET /api/v1/bagua/export`（bagua.py :779-901），异步容器 `ctx.bq_export_jobs` + daemon Thread（:96-252），review_rules 三态语义（None/[]/非空）
- 同卦：`POST /api/v1/bagua/same-gua`（:625-671）；同日柱 `same-rizhu`（:734-777）；成分股 `constituents`（:521-547）
- 规则：`RuleService.list_rules(include_hidden)`（`service/rules.py` :186）；`IndicatorSpec`（`indicators/models.py` :24）
- 筛选内核：`indicator_review.run_weekly_review(persist=False)`（`service/indicator_review.py` :285）——persist=False 不读不写 `review_{asof}.json`
- **已确认缺陷**：indicator_review.py :337-350，`persist=False` 且数据面不可用（surface=None）时仍会回读已有 ok 缓存返回——本计划修复为直接返回 no_go
- 标的目录：`data/universe.py` `to_std_code` :88；名称缓存 `service/stock_names.py` :180；指数/ETF `service/index_etf.py` watchlist
- 日历/覆盖：`GET /api/v1/calendar/range`（`api_routes/system.py` :888）；周/月封口 `data/periods.py` `aggregate_week/aggregate_month`

## 六、基线截图

正式页面基线截图在实现前采集，归档至本目录 `baseline/`（若验证环境具备浏览器条件）。
