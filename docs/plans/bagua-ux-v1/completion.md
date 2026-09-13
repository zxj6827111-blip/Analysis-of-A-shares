# PLAN-BAGUA-UX-V1.1 逐项完成表（本地 ZCODE 执行记录）

> **2026-09-12 第三次更新（R3）**：Codex 第三轮复核结论为「核心功能可用，但暂不建议直接部署」
> （2 项上线前必修 + 1 项非阻断，见 [review-r3-codex.md](review-r3-codex.md)）。
> 已完成整改并重跑复核给的两个反例确认消失，详见 **[rectification-r3.md](rectification-r3.md)**：
> ① 筛选条件版本化——条件变更（含「从查询结果导入」）即让旧结果/导出入口失效并标注旧条件，
> 在途结果晚到也只按过期呈现；② 导出与筛选各自独立 submitting 状态 + 入口防重入
> （切页签不再放开按钮，二次点击不再发第二个 POST）；③ 筛选多候选剔除指数/ETF。

> **2026-09-12 第二次更新（R2）**：Codex 第二轮复核判定「整合方向正确，但整体本地验收仍为 NO-GO」
> （11 项问题，见 [review-r2-codex.md](review-r2-codex.md)）。已完成第二轮整改并重新验证，
> 详见 **[rectification-r2.md](rectification-r2.md)**（另含 /review 自检补充 4 项）。本文件下方 R1 阶段的结论中，以下**已被 R2 推翻或修正**，
> 以 rectification-r2.md 为准：
>
> | 本文件原结论 | R2 核实结果 |
> |---|---|
> | 「指定范围空拒绝」内核修复成立 | 内核成立，但入队参数把全市场 `None` 归一成 `[]`，**全市场被误拒**；已修（R2-01） |
> | 三入口/详情迁入完成 | 迁入成立，但**同卦查询口径**仍按大小写比较类型，前复权详情退化为未复权；已修（R2-02） |
> | 周/月日期换算完成 | 月正确；**部分年份 ISO 周整体错一周**（2021-W01 算成 2020 年那周）；已修（R2-03） |
> | 「getCurrentModule allowed 列表补 workbench」✅ | 仅入白名单未做视图映射，**旧链接打开空白页**；已修（R2-04） |
> | 「390px 无溢出/单列」✅（以 scrollWidth==clientWidth 为据） | 无横向溢出成立，但**主导航被挤成 ≈19px**；该断言不足以证明移动端可用；已修（R2-11） |
> | 「任务恢复/失效提示 404 → 任务记录已失效」✅ | 导出侧已修；**筛选轮询把所有异常当 404**，503 被误判失效且不重试；已修（R2-07） |
> | 「导出快照传递」✅ | 快照转移成立，但**部分成功时按钮文案与实提交范围不一致**、筛选→导出未继承口径；已修（R2-09/R2-10） |
> | 「导出清理保护运行中任务」✅ | 导出入口已修；**同卦入口仍会删运行中记录**（共用容器）；已修（R2-08） |
> | 导出规则选择保持（R1） | 筛选侧成立；**导出侧切换页签即丢勾选**；已修（R2-06） |
>
> 测试口径更正：R1 记录的「1807 passed」未复现；R3 整改后完整复跑 `tests/apps/astock`
> 为 **1841 passed, 6 skipped**（6 skip 均为环境数据类）。

> **2026-09-12 第一次更新（R1）**：Codex 复核判定首轮实现「部分完成，验收不通过」（9 项问题 + 3 项补充）。
> 已全部整改并重新验收，详见 **[rectification-r1.md](rectification-r1.md)**（整改对照表 + 验证证据 + 整改后截图 v2-*）。
> 整改要点：主导航仅保留「查询卦象」（三页签内嵌）、周/月日期换算、混合口径逐项拆分、
> 规则勾选独立状态、快照 token、指定范围空拒绝、失败/断连状态、统计按票去重、
> 导出清理保护与文件名唯一、详情完整迁入（卦辞/爻辞/高岛断语）、同卦/同日柱/成分股就地完成。

分支：`feature/bagua-workbench-ux`（自 `feature/bagua-export-rule-picks` @ 89e31df 新开，未提交，等待用户/Codex 复核）
工作区：所有改动未 commit（遵循「验证通过后才 commit」约定）

## 一、三个遗留修复

| 项 | 状态 | 说明 |
|---|---|---|
| fastapi/starlette 钉版本 | ✅ | requirements.txt：`fastapi==0.135.1`、`starlette==0.52.1`、`uvicorn>=0.23.0,<1`（与本地运行版本一致） |
| 公式收进测试 fixture | ✅ | `tests/fixtures/formulas/{735金叉及趋势,先跌后涨新版5日外}.txt` 入库；conftest `formula_indicator_dir()` 真实 指标/ 优先、fixture 兜底；13 个公式测试 CI 不再跳过 |
| persist=False no_go 不回读缓存 | ✅ | indicator_review.py no_go 分支的缓存读/写全部以 `persist` 为前提；新增回归测试 `test_review_persist_false_no_go_never_reuses_cached_ok` |

## 二、后端增量接口

| 接口 | 状态 | 实现位置 |
|---|---|---|
| GET /api/v1/bagua/instruments | ✅ | screening.search_instruments：股票票池（与筛选/导出同源）+ 指数/ETF 预置，六位代码歧义返回全部候选（000001 → 平安银行 + 上证指数），支持名称/前缀/sh 前缀别名 |
| GET /api/v1/bagua/options | ✅ | screening.instrument_options：标的实际行情覆盖（first/last）、数据面最新日、周/月最近完整周期（closed 标注）、可用价格口径（指数/ETF 固定未复权） |
| GET /api/v1/bagua/screen/rules | ✅ | screening.list_screen_rules：可执行判定（tdx_formula+signal+DAY+ready+无 MIN1），不可执行带原因；包含隐藏规则 |
| POST /api/v1/bagua/screen | ✅ | 独立校验（规则存在且可执行/日期严格/scope）；数据面与日期提交时预检（503/400+建议日期）；任务入独立容器 |
| GET /api/v1/bagua/screen/jobs、/{id}、/{id}/result | ✅ | 列表/状态（不含 result 防轮询膨胀）/完成结果；404 文案含「已失效」 |
| POST/GET /api/v1/bagua/export + force_async | ✅ | 默认 False，旧调用行为不变；force_async=true 无视 limit<=50 强制后台 |
| 导出任务记录 | ✅ | 新增 scope_summary/codes 快照（≤300）/codes_omitted/created_hm/review_rules_mode；不暴露文件路径 |
| 筛选任务容器 | ✅ | 独立内存 dict + 单工作线程 + queue.Queue(5)，满则 429；记录清理不删执行中任务 |
| run_weekly_review 质量统计 | ✅ | 新增 missing_count / failed_codes（向后兼容增量），筛选侧「未完成评估」依据 |

## 三、前端工作台（index_v3.html）

| 项 | 状态 | 验证 |
|---|---|---|
| 导航入口「卦象工作台」+ ?module=workbench | ✅ | getCurrentModule allowed 列表补 workbench |
| 三入口顺序/选中态/用途说明/状态保留 | ✅ | 截图 + DOM 断言 |
| 查卦象：候选/歧义/批量/防抖乱序防护 | ✅ | seq 防乱序 + 浏览器实测（000001 歧义、批量逐项反馈） |
| 查卦象：周期时间/数据日期/口径 | ✅ | 日期全部来自 options 真实覆盖（surface_max_date=20260911）；指数/ETF 自动未复权并说明 |
| 查询结果：主结论先行/详情分层/同卦同日柱成分股入口 | ✅ | 结果行（主卦动爻）→「查看详情」展开分层；跳转旧查询页并带入条件 |
| 条件变更失效提示 | ✅ | 「条件已变更，请重新查询。」+ 结果隐藏 |
| 导出快照传递 | ✅ | 结果区按钮携带 snapshot 进入导出面板（scope/日期/口径/来源标注） |
| 筛选：规则检索/已选可见/any/all/范围导入/严格日期 | ✅ | 浏览器实测 735 × 600033 命中（与周五链复核交叉一致）；零命中无导出按钮 |
| 筛选：质量统计（范围/完成/缺数据/错误/命中） | ✅ | 统计条渲染实测 |
| 导出：三范围/模板说明/附加明细/摘要/记录 | ✅ | 浏览器实测创建任务→完成→记录→下载按钮；附加明细 15 规则可勾选 |
| 任务恢复/失效提示 | ✅（R2 修正） | localStorage + 服务端合并；404 → 「任务记录已失效」（终态）；**筛选轮询区分 404 与暂时断连**，503/超时保留状态并重试，结果获取失败只重取结果（R2-07） |
| 390px 无溢出/单列/入口保留 | ✅（R2 修正） | DOM 断言 scrollWidth==clientWidth（不足）；**主导航改为整行 `flex:1 0 100%` + 横向滚动**，390×844 实测 375px 宽、7 个按钮 76–96×48px、触摸/键盘/点击均可达（R2-11） |
| 原型示例数据/模拟延时/Tweak 未进入正式代码 | ✅ | 代码审查确认 |
| 全市场筛选（scope=all） | ✅（R2 修复） | 任务参数固定 `codes=None`，空指定范围提交前 400；跨层测试覆盖 all/None、picked/[]、ETF-only、混合、重复（R2-01） |
| 资产类型归一化 | ✅（R2 新增） | `wbSymType` 统一 stock/STK、index/IDX、etf/ETF；同卦口径随详情口径（R2-02） |
| ISO 周换算 | ✅（R2 修正） | 1 月 4 日锚定 + 53 周校验；2019–2026 全周回代自洽（R2-03） |
| 旧 `?module=workbench` | ✅（R2 修正） | 规范化为 `bagua-query` 后再切视图（R2-04） |
| 页签独立完成任务 | ✅（R2 新增） | 筛选/指定标的导出可直接输入粘贴股票，不必先查询；摘要标注范围来源 |
| 规则来源/版本展示 | ✅（R2 新增） | `/screen/rules` 增加 source/version，规则行常驻「来源 · 分类 · ID · 版本」，不按名去重 |

## 四、测试

| 套件 | 结果 |
|---|---|
| tests/apps/astock/test_bagua_workbench.py（新增 20 例） | 20 passed |
| tests/apps/astock/test_bagua_workbench_r2.py（R2 24 例 + R3 8 例：跨层链路 + JS 行为/函数级 + 静态不变量） | 32 passed |
| tests/apps/astock/test_indicator_review.py（含 13 个公式测试 + persist 回归） | 全 passed，0 skip |
| **全量 tests/apps/astock（R3 整改后复跑，2026-09-12）** | **1841 passed, 6 skipped**（6 个 skip 均为环境数据类：TDX client / 300040 日线 / forecast xlsx，与本次改动无关） |
| 内联 `<script>` 块 `node --check` | 语法 OK |
| 浏览器真实数据验证 | 查询/详情/关联口径/筛选/导出继承全链路 PASS（数据日 20260911，正式 L1/Tushare 前复权；8767 独立实例，未启动真实全市场任务） |

## 四-B、真实数据全市场导出与首页/控制台验证（2026-09-12 补录）

**全市场导出（all_stocks=true + force_async=true）**：

- 命令：`POST /api/v1/bagua/export?async_mode=true&force_async=true`，body `{"all_stocks": true, "date": "2026-09-11", "periods": ["DAY","WEEK","MONTH"], "adjust": "raw", "review_rules": []}`（工作台口径：附加明细默认关闭发空列表）
- 任务 `bqexp_83f654bc34e4`：queued → running → **done**，message「导出完成」
- 数据日期与规模（meta 工作表 + 任务记录）：`query_date=20260911`（数据面最新交易日，非电脑日期）；requested 6986 = **stock-all 5217 只 A 股 + etf-all 1769 只 ETF**；`ok_total=6986, error_total=0`
- 下载端点：HTTP 200，2,050,438 bytes，`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
- 工作表结构：`meta`（query_date/月卦归属/跨月标注 month_applies=2026-08:适用9/14-9/18/日柱来源/高岛覆盖 379/384）+ `stock-all` + `etf-all`，17 列；与 picked 单票样本**列结构完全一致**（新旧兼容核验通过）
- 归档：`samples/export-all-market-20260911.xlsx`（2.0MB）；单票对照样本 `samples/export-picked-000001-20260911.xlsx`
- 规则版本记录：本轮 `review_rules=[]`（不带信号 sheet，meta 中 indicator_review_note=「select:未勾选任何信号规则」）；信号规则命中口径见 samples 中 picked 样本与周五链 `review_20260911.json`（735/5日外，规则版本=指标/ 目录 txt+tn6，详见 baseline.md §五）

**真实浏览器 `/` 与 `/v3` 验证（IAB，1440×900）**：

| 项 | `/` 首页 | `/v3` |
|---|---|---|
| 渲染 | title「A股研究工作台 · V3」、API 正常、完整渲染（截图 `compare/home-index-1440.png`） | V2.9.7、API 正常、默认视图回测 |
| 控制台 | **0 错误**（iframe 重载捕获 uncaught + console.error/warn） | **0 错误 0 警告**（同法，加载期全覆盖） |

采集方法：页面内创建隐藏 iframe 重新加载目标 URL，在 contentWindow 上挂钩 `onerror` + `console.error/warn` 后等待加载完成统计——覆盖文档加载期全部 JS 错误路径。

## 五、已知边界与未验证项

- 键盘焦点顺序（Tab 遍历）未实测（IAB 无法模拟真实键击）；焦点样式已实现。
- 浏览器原生下载弹窗未实测；下载端点以 HTTP 验证（200 + xlsx）。
- 服务重启后的任务失效为内存语义（计划明确本轮不建持久化任务系统）。
- 筛选全市场大范围耗时与导出类似（分钟级），本轮以小样本验证正确性；R2 的全市场路径
  由跨层测试覆盖（仅替换行情与公式计算），**未跑真实全市场筛选**。
- IAB 截图捕获超时：R2 的移动端导航证据为几何测量 + 真实点击切换，非截图。
- 结果端点（`/screen/jobs/{id}/result`）单独注入 503 的恢复路径未实测，仅 503 状态端点注入实测。

## 回退方法

分支未合并、未推送；`git checkout main`（或原分支）即回退。改动文件清单见 `git status --short` 相对 89e31df。
