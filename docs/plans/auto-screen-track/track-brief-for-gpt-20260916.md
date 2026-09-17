# 跟踪模块整改资料包（按 GPT 模板填写）

> 生成日期：2026-09-16。所有内容以当前工作区代码为准（分支 feature/auto-screen-track），
> 字段名、口径、状态枚举均经代码核实；标注【待用户确认】的是主观/决策项。
> 配套前端全量代码：`docs/plans/auto-screen-track/track-page-extract-for-gpt-20260916.md`
> 口径契约（权威）：`docs/plans/auto-screen-track/contract.md`（v4.1）

## 先纠正几个预设（与你的清单不符的实际现状）

1. **没有 sparkline / 没有任何图表、没有图表库**。整站无 ECharts，L0 接口也不返回逐周序列，"趋势 sparkline 数据源"目前不存在，属于新增项。
2. **历史补算没有 Drawer**。是 L0 顶部一个原生 `<details>` 折叠面板（默认收起，2026-09-16 用户要求）。全站没有 Drawer 组件。
3. **收益口径切换已被刻意隐藏**。双口径（信号收盘/首日开盘）数据和代码分支都在，但 2026-09-15 用户要求把「信号收盘」切换按钮从 UI 移除、固定展示「首日开盘」（exec）口径，因为用户看不懂两个口径。恢复切换只需加回一个按钮。
4. **基准只有沪深300**，中证500/创业板指/不对比基准都不存在，也没有基准选择器。
5. **三个"页面"其实不是三个页面**：同一个 section 内三块 DOM 互斥显隐（`wbtShowLevel('l0'|'l1'|'l2')`），无独立 URL、无面包屑，刷新/深链只能回 L0。
6. 表格没有统一封装：三级各写各的 `innerHTML` 字符串拼接，只有 L2 有列定义数组（`WBT_L2_COLS`），L0/L1 表头手写在渲染函数里。

## 一、页面资料

无设计稿——页面由代码直接驱动（实现即原型）。三级页面 = 同一 HTML section（`#wbTrackRoot`）内的 `#wbTrackL0 / #wbTrackL1 / #wbTrackL2` 三个 `<div class="wb-block">`（互斥 hidden）。

| 层级 | 定位 | 代码位置（index_v3.html 行号） |
|---|---|---|
| L0 跟踪首页（指标总览） | 一行一个「规则×公式版本」，近 12 信号周等权汇总 | HTML 1809–1904，JS `wbtRenderOverview` 15711 |
| L1 某策略历史周列表（中间页） | 该规则版本最近 26 个信号周逐周表 | JS `wbtRenderRuleWeeks` 15877 |
| L2 某周股票明细页 | 该周入选票逐票表 + 行内「详情」展开 | JS `wbtRenderWeekDetail` 16091，`wbtRowDetailHtml` 16056 |

页面截图：本机 serve（127.0.0.1:8799）当前未启动，需要时可补拍三张截图。

## 二、前端实现

1. 使用框架：**无**。原生 JS 单文件 SPA，一个 IIFE，16640 行
2. UI 组件库：**无（自研）**。`.wb-*` 类名 + CSS 变量（`--wb-line/--wb-soft/--red/--green/--muted`），深色主题；样式体系与「卦象」工作台共用（`.wb-root` 前缀），跟踪专用 CSS 仅 14 行（表格/警示框/涨跌色）
3. 图表库：**无**。`web/static/vendor/` 存在（第三方脚本本地化目录，可放 ECharts，无构建链、只能 script 标签引入）
4. 相关页面文件路径：
   - 前端唯一文件：`wtpy/apps/astock/web/static/index_v3.html`
   - 后端路由：`wtpy/apps/astock/api_routes/tracking.py`（L0/L1/L2/export）、`track_backfill.py`（补算）
   - 服务层：`wtpy/apps/astock/service/screen_tracking.py`（结算与聚合）、`screen_contract.py`（口径算法权威实现）、`track_export.py`（xlsx）
5. 相关公共组件/工具：`$(id)`、`esc()`、`toast()`、`api()`（fetch 封装，网络错误与 HTTP 错误区分）、`wbFmtDate`；表格/筛选/Drawer **均无复用组件**，筛选页与补算的规则勾选（检索+复选+chips）是同款模式的两份手写实现

## 三、数据字段（全部为接口实际字段 → UI 列名）

### 1. L0 首页总览表（`GET /api/v1/bagua/track/rules?weeks=12`）

| UI 列 | 接口字段 | 说明 |
|---|---|---|
| 指标 | `rule_name`/`rule_id`/`fingerprint` | 名称下小字显示版本前 6 位；同公式不同 rule_id 按指纹归并成一行 |
| 本周入选 | `latest_week`+`latest_selected` | 按钮，点击直达该周 L2 |
| 跟踪/已结算 | `tracked_weeks`/`settled_weeks` | `subset_weeks`>0 时 ⓘ 角标（部分周来自指定规则补算） |
| 总票次 | `total_selected` | `insufficient_sample` 时黄色「样本不足」 |
| 近12周胜率(首日开盘) | `weekly_equal_win_rate_exec` | sig 版 `weekly_equal_win_rate_sig` 仍在产物里 |
| 近12周平均收益(首日开盘) | `weekly_equal_mean_ret_exec` | 同上双口径 |
| 近12周平均超额 | `weekly_equal_mean_excess_sig` | **只有信号口径**（无 exec 版聚合） |
| 有效周数 | `weekly_equal_valid_weeks_exec` | 等权只聚合 valid>0 的周 |
| 操作 | — | 「查看历史 →」进 L1 |

默认排序 win_rate 降序；无 KPI 卡片区、无逐周序列（sparkline 无数据源，需新增接口字段或由 L1 接口拼装）。

### 2. L1 中间页（`GET .../track/rules/{rule_id}/weeks?weeks=26&fingerprint=`）

顶部摘要条复用 L0 行数据（不再请求）。表列：

| UI 列 | 接口字段 |
|---|---|
| 信号日（周） | `week_id`（YYYYMMDD=该周最后交易日）+ 标签 `run_kind`（周五链/回填/重算）+「指定规则补算」标签（`rules_scope=subset`） |
| 入选 | `selected_count` |
| 平均收益(首日开盘) | `aggregate.mean_ret_close_exec` |
| 胜率(首日开盘) | `aggregate.win_rate_exec` |
| 平均最大涨幅 | `aggregate.mean_max_gain_sig`（周内最高价相对**信号日收盘**，与收益口径无关） |
| 平均回吐 | `aggregate.mean_giveback_sig` = mean(max_gain_sig − ret_close_sig) |
| 平均超额 | `aggregate.mean_excess_sig`（相对沪深300，仅信号口径） |
| 状态 | `completion` ∈ 已结算/待结算/基准缺失/整周休市/失败/未生成产物/数据版本变更 |
| 操作 | 「周明细 →」 |

「是否回填」= `run_kind==='backfill'` 标签；「是否有产物」= completion 已达终态（产物与状态一体，无独立标志）；表底固定一句提示：已结算≠数据完整，覆盖率进明细看。

### 3. L2 周明细（`GET .../track/weeks/{entry_asof}?rule_id=`）

页头：信号周日期、completion/运行类型/回填/子集标签、跟踪周日期范围+交易日数+短周、snapshot_id、**分口径覆盖率三项**（signal_close/week_first_open/excess，<0.9 黄色警示）、回填免责全文（契约 §9）、子集范围提示（后端单一来源文案）、pending 时的「进行中浮动值」警示、待结算 chips（`pending_picks`）。

主表默认 9 列（此前 18 列平铺被用户嫌密，2026-09-15 精简）：

| UI 列 | 接口字段 |
|---|---|
| 代码 | `code_disp`（code 去市场后缀） |
| 名称 | `name`（名称快照） |
| 周一开盘价 | `entry_open_week`（重读前复权，exec 口径买入基准） |
| 周五收盘价 | `close_week_end`（跟踪周最后交易日收盘） |
| 最高涨幅 | `max_gain_sig` + 见顶星期（`max_gain_sig_date`） |
| 本周涨幅 | `ret_close_exec`（未结算票显示「进行中」） |
| 可成交性 | `fill_status` ∈ 可成交/一字涨停不可买/停牌·缺数据/判定元数据不足 |
| 状态 | `status`（ok/no_bar/pending）→ 已结算/无行情/进行中 |
| 操作 | 「详情」行内展开 +「查卦象」跨栏目跳转 |

「详情」展开行（=你说的"日收益走势"现状，逐日表而非曲线）：入场价（信号日收盘 `entry_close_signal`）、`min_low_ret_sig`（最低相对入场收益，可正）、`drawdown_close_sig`（**收盘序列**峰谷回撤）、双口径周五收益（`ret_close_sig`/`ret_close_exec`）、理论开盘收益（`theoretical_open_ret`，买不进项参考值不计统计）、沪深300（`bench_ret_sig/exec`）、超额（`excess_sig/exec`）、逐日表：每交易日 [相对信号日收盘涨幅, 收盘, 停牌/缺数据]（逐日字段还有 `ret_vs_week_open`/`high`/`ret_high_vs_signal` 未全部展示）。

## 四、业务口径（权威：contract.md v4.1，算法实现 `service/screen_contract.py`+`screen_tracking.py`）

1. **收益口径两种**（均「次周窗口内」卖出=窗口最后交易日收盘）：
   - 信号收盘（sig，理论口径）：信号日收盘买入 → 跟踪周末收盘；
   - 首日开盘（exec，成交假设口径）：跟踪周首日开盘买入 → 周末收盘；一字涨停买不进（`limit_up_unbuyable`）与停牌无 K 线（`no_bar`）**不进分母、收益 null**。
   - 「策略实际退出价」口径不存在。UI 现在固定 exec，sig 数据仍在产物字段里。
2. **基准**：只有沪深300，双窗口（sig→周末收盘、exec→周末收盘，各自同期）。无中证500/创业板指/不对比选项。基准缺失 → excess=null + completion=blocked_benchmark（只补基准+超额产生新 revision）。
3. **超额收益** = 同期同口径策略收益 − 沪深300 同期收益；有效样本单独计数（`excess_valid_count`），不借用收益分母；**聚合层只提供 sig 口径**。
4. **回吐** = max_gain_sig − ret_close_sig 的均值（冲高回落的描述统计，契约明确不可由此推导卖出规则）；**最大回撤是两个字段**：最低相对入场收益 = min(low)/entry−1（可为正，如实显示）、峰谷回撤 = 基于逐日**收盘**序列（日 OHLC 无法确定同日高低先后，故回撤按收盘算，命名即 `drawdown_close_sig`）。
5. **「已结算」定义** = `completion==='complete'`，**只表示这轮跟踪评估跑完了，不表示数据完整**；数据完整度 = 分口径覆盖率（<0.9 UI 黄色警示，绝不渲染成绿色"已结算"——这是契约 §1/§6 硬约束）。整周休市 `no_trading_week` 是合法终态（退出码 0）。注意与「快照发布」是两回事：发布门槛=no_data 比例≤5%（可配置默认值）+ 索引发布指针，统计只认指针。
6. 跨周聚合默认**每周等权**（票次加权后端也有但不展示）；「近 12 周」= 最近 12 个信号**自然周**（周历定义，不是倒找 12 个有收益的周）；胜率分母=对应口径 valid 数，空仓周胜率=null（显示「—」，不是 0）；同规则改公式按指纹分段隔离历史。

## 五、核心使用场景（【部分待用户确认】）

- 使用者：**主要用户自己**（个人研究工作台，单机部署在云 VM 上，自己访问）；另有对外 REST API 文档（`docs/api_partners.md`）给合作方读数，但跟踪 UI 不给终端客户演示。**不是团队多人协作工具，无权限体系。**
- 数据生产是「半生产运营」：每周五 18:30 自动链（全市场扫描→发布快照→跟踪结算→复核），页面同时是这条链的结果查看器。
- 高频任务排序：① 看哪条策略最近有效（默认按 12 周胜率降序）；② 回看某策略逐周表现（L1）；③ 验证具体某周选出哪些票、逐票走势（L2）；④ 指定规则补算历史周（低频但很重要——单规则约 9~11 分钟/周，全量周 64~71 分钟）；⑤ 明细行跳「查卦象」（策略与六爻信号交叉验证，是该模块特有交互）。

## 六、当前不满意点清单（候选，来自代码观察与历史反馈，【需用户勾选确认】）

1. 无趋势表达：L0 只有 12 周聚合单值，判断策略"变好还是变差"必须逐条点进 L1，无 sparkline。
2. 三级钻取无 URL/面包屑：不能分享「某规则某周」链接，刷新回 L0；层级切换靠显隐，用户可能没意识到"进了一层页"。
3. 补算面板在总览表上方，展开把主表推下去；补算任务进度只在面板底部小字区，离开栏目后只能靠回到栏目才续上轮询。
4. 口径概念仍有三处解释负担：列头标(首日开盘)、L2 详情双口径并列、超额只有 sig 口径与相邻收益列口径不一致——上一轮只解决了"标注"，没解决"理解"。
5. L0 信息无视觉重点：9 列文字权重相同，胜率/平均收益这两个最该跳出来的数字不突出；表头文案长（"近12周平均收益(首日开盘)"）。
6. L1 信息薄：逐周一行只有均值类数字，看不到当周收益分布/最好最差票，也不能整行点击（只有操作列按钮）。
7. L2「详情」是行下展开（colspan 大单元格），逐日表+8 个 stats 挤在展开区，不是右侧 Drawer，宽表下体验受限。
8. 「未结算票」与「进行中周」的黄色系警示样式较朴素（纯文字+黄框），扫一眼难分辨哪些周是浮动值。

## 七、技术限制

1. 桌面端优先，深色主题必须沿用（与卦象工作台共享 `.wb-root` 样式与 CSS 变量），不能改亮色。
2. **不引入框架/组件库**：无构建链，全站 16640 行原生 JS 单文件；引 React/Antd = 重写全站，不现实。可行的升级上限 = 原生 DOM+CSS 重构 + 可选本地化脚本库（vendor 目录，ECharts 属可引入，但需离线文件，服务器不便时图表也可用纯 SVG/CSS）。
3. 后端 FastAPI 模块化路由，**新增接口便宜**（`api_routes/` 新文件注册即可）；但**统计口径不能随便动**：跟踪产物是不可变文件（`schema_version`/`algo_version` 版本化，字段变更须 bump revision 保旧文件），聚合改动会牵动 xlsx 导出与对外 API 对账（有过 UI/导出同名列两套算法的事故，已闭环为单一算法）。
4. Python 改动需重启 serve（本机 8799；生产 systemd astock-serve@云VM），静态页热更新；服务器 8GB、重任务必须串行（OOM 前科）。
5. 用户 UI 偏好（明确提过两次的要求）：次要入口默认 `<details>` 折叠、口径/免责文字不做常驻段落（放 tooltip 或导出文件里）、表格是主角别被说明区挤压。

## 八、本轮目标（建议值，【待用户确认】）

1. 范围：只动「跟踪」模块 3 层（L0/L1/L2）+ 补算交互，一次做完（同文件同风格，拆开做反而不统一）。
2. 性质：**视觉 + 信息架构 + 交互逻辑**；不动统计口径定义、不动产物 schema；允许新增前端组件（面包屑/状态标签体系/sparkline 组件）与只读聚合接口（如 L0 增加逐周序列字段）。
3. 先出「可上线的落地稿」（受第七节限制约束），完整理想稿可作为附注但不作为交付。
