# A股研究工作台「跟踪」模块 UI/UX 整改与 1:1 实施方案 V1.1（V1视觉保持 + 代码契约对齐版）

> **用途**：本文件是给本地 AI / Codex / ZCODE / Claude Code / OpenCode 等编码代理直接读取和执行的最终实施基线。V1.1 不推翻 V1 的整体版式，而是保持 V1 的清晰度、横向信息密度、深海蓝视觉与三级钻取结构，在读取当前 `feature/auto-screen-track` 分支代码与业务契约后，对字段、口径、补算 Drawer、短周语义与状态提示进行校正。**严禁为了“设计感”新增左侧纵向 Sidebar、重做全局导航或把页面改成另一套产品。**
>
> **仓库**：`zxj6827111-blip/Analysis-of-A-shares`
>
> **目标分支**：`feature/auto-screen-track`
>
> **基线 commit**：`ee3999aec03a1881f33a0a23f55e81e9a0441f8c`
>
> **生成日期**：2026-09-16
>
> **视觉基准尺寸**：V1.1 四张最终效果图均为 **1708 × 921 px**。实施完成后必须在 1708×921 浏览器 viewport 下逐张做 1:1 截图比对；1440/1672/1920 仅作为兼容性检查，不替代 1708×921 的主验收。

---

## 0. 视觉附件与页面映射

本方案必须和以下 4 张图一起交给本地 AI。**图片决定视觉层级与布局，代码契约决定数据语义；当图片中的示例数字或图片生成文字与真实代码不一致时，以代码契约为准。**

1. `assets/01_track_l0_overview_v1_1.png`  
   页面：L0 跟踪首页 / 指标总览
2. `assets/02_track_backfill_drawer_v1_1.png`  
   页面：历史补算 Drawer（从 L0 打开）
3. `assets/03_track_l1_history_v1_1.png`  
   页面：L1 单策略历史信号周列表
4. `assets/04_track_l2_detail_v1_1.png`  
   页面：L2 单周股票明细 + 行展开

### 0.1 对图片的强制解释规则

- **不要照抄图片里的示例数值**。所有数字必须来自真实 API。
- 图片中的“周一/周五”如果和真实短周语义冲突，代码实现必须用“首日/期末”或真实交易日。
- 图片里若出现不存在的版本号（例如 `v1.0.0`），不要伪造；规则版本展示使用真实 `fingerprint` 前 6～8 位。
- 图片里的顶部主导航只作为视觉位置参考，**不得改动现有全局主导航业务结构**；**不得新增左侧纵向 Sidebar**。
- 图片中的收益颜色采用 A 股习惯：**红涨、绿跌**。
- 图片里的“平均超额”最终必须统一为**首个交易日开盘口径**，不能继续把 `sig` 超额伪装成和 `exec` 收益同口径。
- 图片中若写“周一开盘/周五收盘”，正式 UI 用：
  - `首日开盘`
  - `期末收盘`
  并通过 tooltip 解释“期末=该跟踪自然周最后一个实际交易日”。

---

## 0.2 V1.1 相对旧 V1 的强制变更

本节优先级高于后续任何视觉描述。若后续文字与本节冲突，以本节为准。

### A. 页面骨架保持 V1，不做结构性扩张

V1.1 的原则不是“重新设计一套后台”，而是：

```text
保留现有顶部主导航
        ↓
L0 跟踪首页
        ↓
L1 单指标历史周
        ↓
L2 单周股票明细

L0 的历史补算按钮 → 右侧 Drawer
```

**禁止新增左侧纵向 Sidebar。** 最新效果图中如果模型生成了类似“工作台 / 回测分析 / 跟踪监控 / 主线地图 ...”的左栏，一律视为图片生成噪声，不得实现。

### B. 全局导航 HARD CONSTRAINT

必须沿用仓库当前顶部一级导航结构和交互。允许做的只有：

- 保持 `跟踪` 激活态；
- 在跟踪内容区内部调整布局；
- 使用现有顶部导航高度作为页面内容与 Drawer 的定位基准。

禁止：

- 新增左侧导航；
- 把顶部导航复制到侧栏；
- 重命名或重排整个站点一级模块；
- 新增“工作台 / 数据中心 / 系统设置”等本轮不存在的一级入口；
- 为匹配图片伪造月度额度、用户头像、主题开关等非本轮功能。

### C. V1.1 图片的解释方式

四张 V1.1 图片只负责：

- 页面横向比例；
- 信息层级；
- KPI/表格/Drawer 的相对位置；
- 深海蓝色系；
- 表格密度；
- 按钮、Tag、警示条和展开详情的视觉强弱。

图片中 AI 生成的示例数字、日期、星期、股票、版本号、额外快捷区块均不是业务事实。**真实实现只能使用 API/契约支持的数据。**

### D. V1.1 不强制实现的图片噪声

若效果图中出现以下元素，除非当前代码已有同等功能，否则不要新增：

- 永久在线“系统提示”大卡片；
- 重复的“快速操作”宫格；
- 额外右侧常驻说明栏；
- 全站用户中心/月亮主题按钮；
- 未定义的筛选器或状态筛选；
- 图片生成的虚构版本号。

目标是**V1 的主版式 + 当前真实业务内容**，不是逐像素复刻生成模型的幻觉控件。

### E. 主业务口径保持代码对齐

- 主收益：`exec`；
- 主胜率：`exec`；
- 主超额：必须补齐 `exec` 聚合；
- benchmark 固定沪深300，不做下拉；
- 主 UI 不恢复 sig/exec 切换；
- 短周按真实 `track_week_dates`；
- “已结算”不等于 coverage 100%；
- backfill 与 subset 是两个不同维度，必须分别表达。

---

# 1. 当前代码基线与事实源

## 1.1 事实源优先级

本次整改发生冲突时按以下顺序裁决：

1. `docs/plans/auto-screen-track/contract.md`
2. `wtpy/apps/astock/service/screen_contract.py`
3. `wtpy/apps/astock/service/screen_tracking.py`
4. `wtpy/apps/astock/api_routes/tracking.py`
5. `wtpy/apps/astock/api_routes/track_backfill.py`
6. `wtpy/apps/astock/service/track_export.py`
7. `wtpy/apps/astock/web/static/index_v3.html`
8. `tests/apps/astock/test_track_*.py`
9. 本文与 4 张效果图

**原因**：图是 UI 目标，不是业务契约；不可为了视觉 1:1 破坏快照、自然周、分母、回填、发布指针、成交性等语义。

## 1.2 当前技术栈约束

当前跟踪前端不是 React/Vue，不使用 Ant Design/Element/ECharts。当前实现是：

- 单页原生 HTML + CSS + JavaScript；
- 主文件：`wtpy/apps/astock/web/static/index_v3.html`；
- 跟踪视图容器：`#view-track`；
- 工作台样式作用域：`.wb-root`；
- 跟踪三级钻取：
  - `#wbTrackL0`
  - `#wbTrackL1`
  - `#wbTrackL2`
- 现有状态对象：`wbt`；
- 后端：FastAPI；
- 不引入新前端框架；
- 不为 Sparkline 引入 ECharts，直接用原生 SVG。

**本轮整改应保持这一技术路线。**

---

# 2. 本轮整改目标

## 2.1 用户路径

跟踪模块的唯一主路径：

```text
L0 指标总览
  ↓ 点击“查看详情”
L1 某指标历史信号周
  ↓ 点击“周明细”
L2 某指标 × 某信号周 × 个股明细
```

辅助路径：

```text
L0
  ↓ 点击“历史补算”
右侧 Drawer
  ├─ 指定历史周（全部规则 / 最多 5 条指定规则）
  └─ 批量最近 N 周（全部规则）
```

## 2.2 核心整改目标

1. **统一视觉层级**：L0/L1/L2 使用同一套深海蓝研究工作台视觉语言。
2. **减少边框噪声**：从“表格+details堆叠”变成“标题/KPI/工具条/主表”的固定节奏。
3. **历史补算脱离主内容流**：从 `<details>` 改右侧 Drawer。
4. **固定收益口径**：展示层统一以 `exec`（跟踪周首个交易日开盘）为主。
5. **统一超额口径**：UI 主显示的超额必须使用 `excess_exec`。
6. **自然周语义正确**：不再把短周写死成“周一/周五”。
7. **L0 能快速判断趋势**：增加最近 5 周 Sparkline。
8. **L1 变成真正的“中间层”**：只回答“这个策略哪几周好/坏/是否结算/数据从哪来”。
9. **L2 变成研究页面**：主表精简 + 行展开显示逐日表现和诊断指标。
10. **不破坏不可变产物**：优先在只读聚合层派生 UI 所需统计，不因为 UI 改造强制重算历史产物。

---

# 3. 明确不做的事情

本轮禁止顺手扩大范围：

- 不引入 React/Vue。
- **不新增任何左侧纵向 Sidebar / 二级全局导航。**
- 不把跟踪页改成“左导航 + 右内容”的后台模板。
- 不新增常驻右侧说明栏；右侧仅允许“历史补算 Drawer”在用户主动打开时出现。
- 不为效果图中的装饰控件新增不存在的业务功能。
- 不重写整个 `index_v3.html`。
- 不修改周五链核心调度机制。
- 不修改发布指针“不自动覆盖”的契约。
- 不增加“覆盖已有结果”按钮。
- 不增加“跳过已有结果”开关。
- 不增加 benchmark 下拉选择器。
- 不恢复“信号收盘 / 首日开盘”切换器。
- 不为页面展示创建新的 mutable 业务产物。
- 不把 `complete` 理解为“100% 数据完整”。
- 不在 UI 中把回填结果描述成“当时真实发布名单”。
- 不把 `null` 显示为 `0`。

---

# 4. 视觉系统：1:1 复原标准

## 4.1 基准 viewport

```text
Reference viewport: 1708px × 921px（唯一主视觉验收尺寸）
Desktop compatibility: 1440px / 1672px / 1920px
Minimum supported desktop width: 1200px
```

在 1708px 下：

- **不得预留左侧 Sidebar 宽度**；内容区从现有页面左边距直接开始；
- 页面左右外边距：24～30px；
- 顶部全局导航沿用现有高度，不重画主导航；
- `view-track` 内容区横向填满；
- 主内容不设置窄 `max-width`；
- KPI 与表格对齐同一左右边线。

## 4.2 建议颜色 Token

以下为接近 V1 图的目标色，可在现有 CSS 变量基础上复用/映射，不要求建立新主题系统：

```css
--wbt-bg:          #071423;
--wbt-bg-deep:     #06111f;
--wbt-panel:       #0b1b2f;
--wbt-panel-2:     #0e223a;
--wbt-panel-soft:  #102844;
--wbt-line:        #24425f;
--wbt-line-soft:   #19344f;
--wbt-text:        #f3f7fc;
--wbt-text-2:      #c5d4e5;
--wbt-muted:       #829bb6;
--wbt-blue:        #2f86ff;
--wbt-blue-2:      #50a0ff;
--wbt-blue-soft:   rgba(47,134,255,.12);
--wbt-pos:         #ff3c6f;   /* A股：涨=红 */
--wbt-neg:         #00d3a1;   /* A股：跌=绿 */
--wbt-warn:        #f5b63e;
--wbt-warn-bg:     rgba(245,182,62,.10);
--wbt-danger:      #ff5d6c;
--wbt-ok:          #30d59a;
```

如果现有 `--red` / `--green` / `--blue2` 已接近，优先复用，避免全站风格漂移。

## 4.3 字体

```css
font-family:
  Inter,
  "PingFang SC",
  "Microsoft YaHei",
  "Noto Sans CJK SC",
  system-ui,
  sans-serif;
```

数字建议：

```css
font-variant-numeric: tabular-nums;
```

使表格中的百分比与价格垂直对齐。

## 4.4 字号与层级

| 元素 | 目标字号 | 粗细 |
|---|---:|---:|
| 页面 H1 | 28px | 700 |
| 页面副标题 | 13px | 400 |
| L1/L2 标题 | 26px | 700 |
| KPI 数字 | 24–28px | 700 |
| KPI 标签 | 13px | 500 |
| 区块标题 | 19–20px | 700 |
| 表头 | 12–13px | 600 |
| 表格正文 | 13px | 400/500 |
| 二级说明 | 11–12px | 400 |
| Tag | 11–12px | 500 |

## 4.5 圆角

```text
大卡片：10px
KPI 卡：9px
输入框：8px
按钮：8px
Tag：6px
Drawer：左侧上/下圆角 0 或 12px（二选一，按图更接近直角大面板）
```

## 4.6 阴影

不要使用夸张玻璃拟态。

建议：

```css
box-shadow: 0 8px 28px rgba(0,0,0,.18);
```

只用于 Drawer 和浮层。普通 KPI 卡不要强阴影。

## 4.7 全局布局硬约束（V1.1）

```text
┌──────────────────────────────────────────────────────────────────────────┐
│  Logo   回测   跟踪   卦象/主线   规则   实验   任务   数据   ...      │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   跟踪内容区：标题 / KPI / 工具栏 / 表格 / 警示 / 展开详情               │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

必须满足：

1. **0px 左侧全局 Sidebar**；
2. L0/L1/L2 都从同一个主内容左边线开始；
3. 顶部主导航高度沿用现有系统，不创建第二套导航；
4. L1/L2 的“返回”是内容区按钮，不是侧栏层级；
5. Drawer 只覆盖内容区右侧，不替换/遮住整个全局顶部导航；
6. KPI/表格宽度尽量铺满内容区，不设置 1200px 以内的窄居中容器；
7. V1.1 追求“横向舒展、表格为主”，不改成卡片瀑布流。

## 4.8 页面密度

V1.1 比普通 SaaS Dashboard 更偏“研究工作台”：

- KPI 卡可以大，但数量受控；
- 表格必须是页面主体；
- 不要在主表两侧长期占用说明栏；
- 说明信息优先放 tooltip、表尾说明或警示条；
- 1708×921 下 L0 应尽量一屏看到 8～9 条策略；
- 1708×921 下 L1 应尽量一屏看到 6～8 个历史周；
- L2 打开一条详情后仍需看到后续股票行，避免展开区域过高。

---

# 5. 全局信息架构与状态机

继续沿用 `wbt.level`：

```js
level: "l0" | "l1" | "l2"
```

必须保持：

- 从 L0 → L1 不刷新页面；
- L1 → L2 不刷新页面；
- 返回时保留上一层数据和滚动语义；
- 异步请求继续保留 `token + condVersion` 防竞态；
- 离开 `track` 栏目停止 backfill status 轮询；
- 回到 `track` 恢复任务状态同步。

新增 UI 状态建议：

```js
bfDrawerOpen: false,
bfMode: "single",       // single | batch
l1Page: 1,
l1PageSize: 10,
```

可选：

```js
l0TrendPoints: 5
```

---

# 6. P0：数据口径先修，再做视觉

这一节必须优先完成。否则 UI 越漂亮，数据误导越严重。

## 6.1 当前问题

当前主 UI 已固定 `exec`：

- 胜率：`weekly_equal_win_rate_exec`
- 平均收益：`weekly_equal_mean_ret_exec`

但 L0/L1 主显示的平均超额仍来自：

```text
mean_excess_sig
weekly_equal_mean_excess_sig
```

而逐票产物中已经存在：

```text
excess_exec
bench_ret_exec
```

因此无需为了 UI 重算全部历史产物。

## 6.2 推荐方案：只读层派生，不 bump tracking schema

### 修改文件

`wtpy/apps/astock/api_routes/tracking.py`

### 新增纯 helper

建议：

```python
def _with_exec_excess_stats(track: Optional[dict], rule_id: str, agg: Optional[dict]) -> Optional[dict]:
    ...
```

语义：

1. `agg is None` 时允许返回 `None`；
2. 复制 `agg`，不要 mutate 产物内原对象；
3. 从 `track.rows` 中筛选：
   - `row.rule_id == rule_id`
   - `row.status == "ok"`
   - `row.excess_exec is not None`
4. 计算：
   - `excess_exec_valid_count`
   - `mean_excess_exec`
5. 可选计算：
   - `win_rate_excess_exec`
6. 保留现有：
   - `mean_excess_sig`
   - `excess_valid_count`
   不删旧字段，保证兼容。

### L0 新增返回字段

每条 rule row 增加：

```json
{
  "weekly_equal_mean_excess_exec": 0.0062,
  "weekly_equal_valid_weeks_excess_exec": 11
}
```

计算方式：

```text
最近窗口内，每个 settled 周先取得该周 mean_excess_exec；
对非 null 的周做每周等权均值。
```

### L1 aggregate

L1 每个 `weeks[]` 的 `aggregate` 在响应前同样补：

```json
{
  "mean_excess_exec": ...,
  "excess_exec_valid_count": ...
}
```

### L2 建议增加 ui_summary

不改 immutable track product，API 响应增加：

```json
"ui_summary": {
  "selected_count": 32,
  "valid_exec_count": 30,
  "win_rate_exec": 0.4667,
  "mean_ret_exec": -0.0435,
  "mean_excess_exec": -0.0264,
  "return_coverage_exec": 0.9375,
  "excess_coverage_exec": 0.9375
}
```

计算规则：

- selected = 当前 rule 过滤后的 `rows + pending_picks` 去重后数量；
- valid exec = `status == ok && ret_close_exec != null`；
- win rate = valid 中 `ret_close_exec > 0` 的比例；
- mean ret = valid exec 均值；
- mean excess = `status == ok && excess_exec != null` 的均值；
- coverage = valid/selected；
- selected=0 时 coverage=null，不要返回 0。

### 为什么不改 `screen_tracking.py` 产品 schema

因为 `excess_exec` 已经逐票落盘。此次只是为了 UI 聚合派生；没有必要因为 UI 改造让所有历史 track product 强制生成新 revision。

---

# 7. L0 跟踪首页：1:1 实施规格

参考图（V1.1 最终视觉）：`assets/01_track_l0_overview_v1_1.png`

## 7.1 页面结构

从上到下固定 5 层：

```text
[全局导航，保持现状]

跟踪
副标题

[KPI 1][KPI 2][KPI 3][KPI 4]

最近12个信号周 · 每周等权      [搜索] [历史补算] [导出结果]

[策略总表]

[页底说明 / 更新时间（不新增常驻侧栏）]
```

## 7.2 页面标题

保留：

```text
跟踪
```

副标题建议：

```text
周五信号名单的次周真实表现：按指标查看历史信号周的入选、胜率、收益与趋势，点击进入每周股票明细。
```

不要在副标题里堆“开盘价、收盘价、最高涨幅、本周涨幅”等字段列表。

## 7.3 KPI 四卡

### 卡 1：跟踪策略

```text
跟踪策略
9 个
当前可见规则版本
```

数据：

```js
visibleRows.length
```

不要写死 9。

### 卡 2：最新信号周

```text
最新信号周
2026-09-11
（周五）
```

取所有 L0 row 的 `latest_week` 最大值。

### 卡 3：累计入选

```text
累计入选
3,382 票次
最近窗口内累计命中
```

数据：

```js
sum(rule.total_selected)
```

必须写“票次”，不能说“股票数”，因为同一股票跨周/跨规则会重复。

### 卡 4：已结算

不要使用图片生成的“300只”作为真实语义。

建议：

```text
已结算（近12周）
87 / 96
策略周
```

计算：

```js
settled = sum(r.settled_weeks)
tracked = sum(r.tracked_weeks)
```

如果认为“策略周”太技术，可副标题解释。

> V1.1 效果图中可能出现“已结算指标 8/9”之类示例。除非后端增加并明确 `latest_completion` 语义，否则**不要为了匹配图片创造“已结算指标”新统计**；优先使用现有 `settled_weeks / tracked_weeks` 的可解释口径。

## 7.4 KPI 卡尺寸

1708px viewport 目标：

```css
.wbt-kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  margin: 18px 0 20px;
}

.wbt-kpi-card {
  min-height: 112px;
  padding: 18px 20px;
}
```

卡内 icon 采用现有可用符号/纯 CSS/SVG，禁止为了图标引入第三方包。

## 7.5 工具栏

左：

```text
最近 12 个信号周 · 每周等权  ⓘ
```

tooltip：

```text
最近 N 个信号自然周；跨周聚合默认每周等权。收益与胜率按跟踪周首个实际交易日开盘口径计算。
```

右：

```text
[ 搜索策略名称、代码或关键词... ] [历史补算] [导出结果]
```

### 禁止出现

- 左侧纵向 Sidebar；
- 右侧常驻“统计说明/数据说明”栏；说明应收敛到 tooltip/表尾；
- 收益口径下拉；
- benchmark 下拉；
- “信号周”单周选择器。

## 7.6 L0 表格列

按以下顺序：

```text
#
策略名称
最新周入选
累计入选
已结算/跟踪
近12周胜率
平均收益
平均超额
有效周
近5周趋势
补算说明
操作
```

### 字段映射

| UI | 数据 |
|---|---|
| 策略名称 | `rule_name || wbtRuleName(rule_id) || rule_id` |
| 版本 | `fingerprint.slice(0, 6~8)` |
| 最新周入选 | `latest_selected` |
| 累计入选 | `total_selected` |
| 已结算/跟踪 | `settled_weeks / tracked_weeks` |
| 胜率 | `weekly_equal_win_rate_exec` |
| 平均收益 | `weekly_equal_mean_ret_exec` |
| 平均超额 | **`weekly_equal_mean_excess_exec`** |
| 有效周 | `weekly_equal_valid_weeks_exec` |
| 近5周趋势 | `trend_weeks[].mean_ret_exec` |
| 补算说明 | `subset_weeks > 0 ? 含N个指定规则补算周 : —` |

### 最新周入选交互

数字仍允许直达该策略最新周 L2：

```text
点击 8 → L2
```

但不要做成过强的蓝色大按钮。用可点击数字/轻按钮即可。

### 操作

```text
查看详情 →
```

进入 L1。

## 7.7 策略名 cell

两行：

```text
短线强势启动
追启动，持仓 2~5 天 / 版本 651d17
```

如果无描述，则：

```text
版本 651d17
```

规则 ID 不需要默认大字展示，可放小字或 tooltip。

## 7.8 subset 周提示

当：

```js
r.subset_weeks > 0
```

在策略名下方加小 `ⓘ`：

```text
含 2 个指定规则补算周
```

tooltip：

```text
这些历史周只补算了指定规则，不代表该周其他规则无入选。
```

## 7.9 Sparkline

### API 增加

L0 每个规则返回最后 5 个相关周：

```json
"trend_weeks": [
  {
    "week_id": 20260731,
    "settled": true,
    "mean_ret_exec": 0.0121,
    "win_rate_exec": 0.5625,
    "mean_excess_exec": 0.0098
  }
]
```

数据可直接来自 `seg["weeks"]`，不要为 9 个规则再发 9 个 L1 请求。

### 前端 helper

```js
function wbtSparklineSvg(points, width = 112, height = 28) { ... }
```

要求：

- 仅 SVG polyline/path；
- 5 个点以内也能画；
- null 点断开或跳过；
- 最后整体均值 >0 用红；<0 用绿；
- hover title 显示最近周收益；
- 无有效点显示 `—`。

## 7.10 排序

继续支持表头排序：

- 累计入选；
- 胜率；
- 平均收益；
- 平均超额。

`null` 始终沉底。

## 7.11 L0 空态

保持：

```text
暂无跟踪数据
周五链首次运行后自动生成。
```

不要当红色错误。

---

# 8. 历史补算 Drawer：1:1 实施规格

参考图（V1.1 最终视觉）：`assets/02_track_backfill_drawer_v1_1.png`

这是本轮最明显的结构升级。

## 8.1 移除当前 `<details>` 主结构

当前：

```html
<details id="wbTrackBfBox">...</details>
```

改为：

```text
L0 “历史补算”按钮
  ↓
mask + right drawer
```

为了减少现有 JS 改动，以下已有控件 ID 尽量保留并移动到 Drawer 内：

- `wbTrackBfWeek`
- `wbTrackBfRun`
- `wbTrackBfWeeks`
- `wbTrackBfRunN`
- `wbTrackBfRuleSearch`
- `wbTrackBfRuleList`
- `wbTrackBfRuleChips`
- `wbTrackBfRuleCount`
- `wbTrackBfRuleClear`
- `wbTrackBfJobs`

新增：

```text
wbTrackBfOpen
wbTrackBfMask
wbTrackBfDrawer
wbTrackBfClose
wbTrackBfTabSingle
wbTrackBfTabBatch
wbTrackBfSinglePanel
wbTrackBfBatchPanel
```

## 8.2 Drawer 尺寸

1708×921 下：

```css
width: clamp(500px, 30vw, 560px);
height: calc(100vh - var(--app-topbar-height, 68px));
right: 0;
top: var(--app-topbar-height, 68px);
```

效果图约占右侧 30%～33%；顶部全局导航保持可见。

Mask：

```css
background: rgba(2, 9, 18, .58); /* mask 同样从全局 topbar 下方开始 */
backdrop-filter: blur(1.5px); /* 可选；性能差则删 */
```

## 8.3 Drawer 行为

必须支持：

- 点击历史补算打开；
- 点右上角 `×` 关闭；
- 点 mask 关闭；
- `Esc` 关闭；
- 打开后仅锁主内容滚动（若实现成本低可锁 body，但不得导致顶部导航跳位）；
- 关闭后恢复；
- 不影响后台任务继续执行；
- Drawer 内任务状态可轮询；
- 从任务中心跳进跟踪页时不自动弹 Drawer，除非已有明确需求。

## 8.4 Drawer Header

```text
历史补算                                        ×
```

下面是二段 Tab：

```text
[ 指定历史周 ] [ 批量最近 N 周 ]
```

默认：`指定历史周`。

## 8.5 顶部说明框

```text
ⓘ 补算说明
历史补算基于当前规则和当前可用股票池，按所选历史周重新计算名单与跟踪表现。
结果用于策略历史回测与研究参考，不代表当时实际发布的名单。
```

这不是可折叠高级说明，应常驻。

## 8.6 模式 A：指定历史周

### Section 1 选择信号周

```text
1. 选择信号周
信号周（周五） [ 2026-07-31  📅 ]
```

辅助文案：

```text
必须填写该自然周最后一个交易日；节假日短周可能是周四。
```

提交时继续让后端 `_signal_day_hint` 最终校验。

### Section 2 选择规则范围

radio：

```text
○ 全部规则（使用回填）
● 指定规则（最多 5 条）
```

#### 全部规则

不显示规则选择器。

说明：

```text
会重新扫描该历史周的全部可执行规则，耗时较长。
```

#### 指定规则

显示：

```text
[搜索策略名称/规则 ID...]
已选 2 / 5

☑ 先跌后涨5日筛选指标5日外   651d17
☐ 20日平台突破                4c5419
☑ 趋势回踩低吸（低吸不追高） 77a041
...
```

要求：

- 继续用 `Set` 维护选择，不依赖 DOM；
- 搜索过滤后已选不丢；
- 选第 6 条时当场拦截；
- 可单击整行切换；
- 版本显示 6 位；
- 规则来自 `/api/v1/bagua/screen/rules`；
- 只显示 `executable`。

### Section 3 任务预估

V1 图中的大数字结构保留，但文案按真实能力修正。

#### 指定规则

例如选 2 条：

```text
历史周：2026-07-31
规则：2 条指定规则
类型：按当前规则历史重建
预计耗时：约 16 ~ 22 分钟
```

估算：

```text
8~11 分钟 × 规则数
```

必须加：

```text
实际耗时受机器性能与数据量影响。
```

#### 全部规则

不要继续显示后端旧文案“1~3 分钟”。真实全市场复核已有约 85~90 分钟级实测。

UI 建议：

```text
预计耗时：较长，通常约 60~90+ 分钟/周
```

并注明：

```text
实际耗时取决于规则数、全市场票数、磁盘和 CPU。
```

### sticky footer

```text
[取消] [▶ 开始补算]
```

`开始补算` 为主按钮。

## 8.7 模式 B：批量最近 N 周

Drawer 复用同一个壳。

内容：

```text
1. 选择范围
[ 最近 4 周 ▼ ]

选项：4 / 8 / 12

2. 规则范围
全部规则

ⓘ 批量补算不支持指定规则。

3. 任务说明
批量按当前规则重建最近 N 个历史信号周。
```

按钮：

```text
[取消] [开始批量补算]
```

不要出现规则多选。

## 8.8 不允许加入的控件

- 覆盖已有结果；
- 跳过已有结果；
- 收益口径；
- 沪深300下拉；
- “预计任务 = 周数×规则数”这种误导型数字。

## 8.9 后端提示文案修正

文件：`api_routes/track_backfill.py`

当前全部规则成功响应中的“每周约1~3分钟”需要修正。

建议改成不易过时的文案：

```text
已提交补算。全规则历史重建属于全市场重任务，耗时可能较长；
实际时长受规则数、股票池规模和机器性能影响，任务将在后台执行。
```

指定规则可以保留：

```text
单规则通常约 8~11 分钟，实际耗时以当前环境为准。
```

---

# 9. L1 单策略历史信号周：1:1 实施规格

参考图（V1.1 最终视觉）：`assets/03_track_l1_history_v1_1.png`

## 9.1 页面 Header

**本页继续使用顶部主导航 + 单一内容区；禁止左侧 Sidebar。L1 只做 V1 版式微调，不引入新的分栏导航。**


第一行：

```text
← 返回指标总览
```

第二行：

```text
先跌后涨5日筛选指标5日外   [版本 651d17]
```

右侧可保留：

```text
导出该策略结果
```

如果不准备增加单策略导出按钮，则不要为了匹配图片硬加无功能按钮。可继续只在 L0 导出。

## 9.2 标题下说明

```text
最近 26 个信号周的历史表现；收益、胜率与超额均以跟踪周首个实际交易日开盘为入场口径。
```

这里不放口径切换器。

## 9.3 KPI 五卡

```text
跟踪周     12
已结算     11
胜率       51.76%
平均收益   +0.60%
平均超额   +0.62%
```

数据：

- 跟踪周：`weeks.length` 或 API count；
- 已结算：`completion === complete` 数；
- 胜率：L0 `ruleRow.weekly_equal_win_rate_exec`；
- 平均收益：L0 `ruleRow.weekly_equal_mean_ret_exec`；
- 平均超额：L0 **`ruleRow.weekly_equal_mean_excess_exec`**。

收益和超额必须用红涨绿跌。

## 9.4 L1 表格列

```text
信号周
入选
有效样本
胜率
平均收益
平均超额
状态
数据来源
操作
```

### 字段映射

| UI | 字段 |
|---|---|
| 信号周 | `week_id/asof` |
| 入选 | `selected_count` |
| 有效样本 | `aggregate.valid_exec_count` |
| 胜率 | `aggregate.win_rate_exec` |
| 平均收益 | `aggregate.mean_ret_close_exec` |
| 平均超额 | `aggregate.mean_excess_exec` |
| 状态 | `completion` |
| 数据来源 | `run_kind` + `rules_scope` |

## 9.5 信号周 cell

两行：

```text
2026-09-04
20260904
```

如果 `run_kind=backfill`，加 Tag：

```text
回填
```

如果 `rules_scope=subset`，再加：

```text
指定规则补算
```

两个概念不能互相替代。

## 9.6 状态颜色

- `complete`：蓝灰/青色轻 Tag，不使用“100%完整”的绿色暗示；
- `pending`：黄色；
- `blocked_benchmark`：黄色/橙色；
- `failed`：红色；
- `no_product`：灰色；
- `data_version_changed`：橙色；
- `no_trading_week`：灰蓝。

## 9.7 “已结算”解释

表格下方保留固定说明：

```text
ⓘ “已结算”只表示本轮评估已经结束，不代表数据覆盖率为 100%；覆盖率请进入周明细查看。
```

## 9.8 L1 不再主展示的字段

当前旧页：

- 平均最大涨幅；
- 平均回吐。

建议从主表移除。

原因：这些当前主要基于信号口径统计，和主页面固定 `exec` 口径容易造成误读。

如未来需要，可放“更多统计”抽屉，并明确标注“理论信号口径”。

## 9.9 L1 分页

V1 图使用分页，建议实施为**纯前端分页**：

```text
默认 10 条/页
可选 10 / 20 / 全部
```

后端当前一次返回最近 26 周即可，不新增分页 API。

切换策略时重置 `l1Page=1`。

---

# 10. L2 单周股票明细：1:1 实施规格

参考图（V1.1 最终视觉）：`assets/04_track_l2_detail_v1_1.png`

## 10.1 Header

**本页继续使用顶部主导航 + 单一内容区；禁止左侧 Sidebar。股票详情继续使用“表格行内展开”，不改成右侧详情 Drawer/Modal/新页面。**


```text
← 返回历史周列表

先跌后涨5日筛选指标5日外 | 2026-09-04（周五） [已结算] [周五链]
```

副标题：

```text
跟踪区间：2026-09-07 ~ 2026-09-11（5 个实际交易日）
```

短周时：

```text
跟踪区间：...（3 个实际交易日 · 短周）
```

## 10.2 KPI 五卡

```text
入选股票     32
有效样本     30
胜率         46.67%
平均收益     -4.35%
平均超额     -2.64%
```

全部来自 `ui_summary`。

### 有效样本

必须是 exec 口径有效样本。

不要写“上涨家数 3/9”作为主 KPI；如要展示，可放：

```text
胜率 46.67%
14 / 30
```

分母是有效样本，不是总入选。

## 10.3 Coverage 警示条

当：

```text
return_coverage_exec < 0.90
或
excess_coverage_exec < 0.90
```

显示黄色警示：

```text
⚠ 首日开盘收益覆盖率 86.7%，部分股票因一字涨停、停牌/缺数据或判定未知而未计入统计；“已结算”不代表数据完整。
```

如果 benchmark 缺失：

```text
⚠ 沪深300基准数据缺失/边界不完整，本周股票收益已计算，但超额收益暂不可用。
```

## 10.4 Backfill / subset 警示

### backfill

必须渲染后端 `backfill_notice` 全文，不能前端另复制一版。

### subset

必须渲染 `scope_notice` 全文。

两者同时存在时分两条警示，不合并成一句模糊话。

## 10.5 主表列

```text
代码
名称
首日开盘
期末收盘
最高收益
最大回撤
本周收益
超额收益
成交状态
状态
操作
```

### 字段映射

| UI | 字段 |
|---|---|
| 代码 | `code_disp || code` |
| 名称 | `name || —` |
| 首日开盘 | `entry_open_week` |
| 期末收盘 | `close_week_end` |
| 最高收益 | `max_gain_exec` |
| 最大回撤 | `drawdown_close_sig`，tooltip 解释“按逐日收盘序列峰谷计算” |
| 本周收益 | `ret_close_exec` |
| 超额收益 | `excess_exec` |
| 成交状态 | `fill_status` |
| 状态 | `status/completion` |

### 最高收益日期

在 tooltip 或次级小字显示：

```text
+9.89% · 周四
```

使用 `max_gain_exec_date`。

## 10.6 为什么最大回撤可以保留

当前 `drawdown_close_sig` 实际是**收盘序列峰谷回撤**，不是“相对信号收盘的收益”。其值可作为路径风险指标展示，但名称必须明确：

```text
最大回撤 ⓘ
```

tooltip：

```text
按跟踪窗口内逐日收盘价序列计算的峰谷回撤；日 K 无法判断同日高低先后。
```

## 10.7 行展开

点击：

```text
详情 ▼
```

在该股票主行下方插入整行详情，不跳页面。

布局：

```text
┌───────────────────────────────┬──────────────────────────────────────┐
│ 个股关键指标                   │ 实际跟踪周每日收益                    │
│ 首日开盘  81.00                │ 09/07 周一   09/08 周二 ...          │
│ 期末收盘  85.02                │ +2.14%      -1.32% ...               │
│ 最高收益  +9.89%                │                                      │
│ 最大回撤  -6.21%                │                                      │
│ 本周收益  +4.96%                │                                      │
│ 沪深300   +2.65%                │                                      │
│ 超额收益  +2.31%                │                                      │
└───────────────────────────────┴──────────────────────────────────────┘
```

## 10.8 每日收益口径

旧代码当前可能展示：

```text
逐日（相对信号日收盘）
```

必须改成：

```text
实际跟踪周每日收益（相对首日开盘）
```

数据：

```text
daily[].ret_vs_week_open
```

不是：

```text
daily[].ret_vs_signal_close
```

因为主 UI 已固定 exec。

## 10.9 实际交易日

绝不能写死周一～周五。

按照：

```text
track_week_dates
```

渲染。

例：

```text
09/07 周一
09/08 周二
09/09 周三
09/10 周四
09/11 周五
```

短周则只画真实 2/3/4 个交易日。

## 10.10 查卦象

现有功能必须保留，但降级为次要动作。

建议：

- 主表只显示 `详情`；
- 展开区域底部放 `查卦象 →`；

或：

```text
详情 ▼   ···
```

`···` 中放“查卦象”。

不要删业务功能。

---

# 11. CSS 组件规划

在 `.wb-root` 基础上新增 `.wbt-*`，不要污染全站。**不要新增 `.sidebar` / `.sidenav` / `.left-nav` 一类全局结构。**

建议组件：

```text
.wbt-page-head
.wbt-page-title
.wbt-page-sub
.wbt-kpi-grid
.wbt-kpi-card
.wbt-kpi-icon
.wbt-kpi-label
.wbt-kpi-value
.wbt-kpi-note
.wbt-toolbar
.wbt-toolbar-meta
.wbt-search
.wbt-table-wrap
.wbt-table
.wbt-strategy-cell
.wbt-version
.wbt-source-tag
.wbt-status-tag
.wbt-spark
.wbt-info-line
.wbt-warn-line
.wbt-drawer-mask
.wbt-drawer
.wbt-drawer-head
.wbt-drawer-tabs
.wbt-drawer-tab
.wbt-drawer-body
.wbt-drawer-section
.wbt-drawer-info
.wbt-drawer-footer
.wbt-rule-list
.wbt-rule-item
.wbt-task-estimate
.wbt-l2-expand
.wbt-daily-grid
.wbt-daily-cell
```

## 11.1 表格高度

目标：1708×921 下一屏可以看到 8～9 行策略。

建议：

```css
.wbt-table th { height: 48px; }
.wbt-table td { height: 46px; }
```

策略名两行时可 52px。

## 11.2 Hover

```css
.wbt-table tbody tr:hover {
  background: rgba(47,134,255,.045);
}
```

不要整行发光。

---

# 12. HTML 重构建议

## 12.1 保留三级 DOM ID

必须保留：

```text
wbTrackL0
wbTrackL1
wbTrackL2
```

避免大范围重写 `wbtShowLevel`。

## 12.2 L0 骨架

建议重构成：

```html
<div id="wbTrackL0">
  <div class="wbt-page-head">...</div>
  <div id="wbTrackL0Kpis" class="wbt-kpi-grid"></div>
  <div class="wbt-toolbar">...</div>
  <p id="wbTrackL0Error" ...></p>
  <div id="wbTrackL0List" class="wbt-table-wrap"></div>
  <div id="wbTrackL0Footer" class="wbt-info-line"></div>
</div>
```

## 12.3 Drawer 放在 `view-track` 末尾

不要嵌在表格内部。

```html
<div id="wbTrackBfMask" hidden></div>
<aside id="wbTrackBfDrawer" ... hidden>...</aside>
```

这样 z-index、scroll、focus 更容易管理。Drawer 应挂在 `view-track` 末尾或应用主容器末尾，但定位从顶部主导航下缘开始；不得通过新增左侧导航腾出内容区。

---

# 13. 前端函数级修改清单

文件：`wtpy/apps/astock/web/static/index_v3.html`

## 13.1 保留

- `wbtLoadOverview`
- `wbtOpenRuleWeeks`
- `wbtOpenWeek`
- `wbtShowLevel`
- `wbtBind`
- `wbtPollBackfill`
- `wbtRunBackfill`
- `wbtQueryFromTrack`
- `wbtPct`
- `wbtPctSigned`

## 13.2 重写/显著修改

### `wbtRenderOverview`

职责变为：

1. render KPI；
2. render toolbar meta；
3. search；
4. table；
5. sparkline；
6. subset marker；
7. 不再显示旧的长口径说明。

### `wbtSortValue`

增加：

```text
ret → weekly_equal_mean_ret_exec
excess → weekly_equal_mean_excess_exec
```

旧 `excess -> sig` 必须改。

### `wbtRenderRuleWeeks`

改成 V1 五 KPI + 新表格。

### `wbtRenderWeekDetail`

改成 V1 五 KPI + warning + 新列。

### `wbtRowDetailHtml`

默认展示 `ret_vs_week_open`。

### `wbtRenderBackfillRules`

保留 Set + 搜索逻辑，HTML 改成 Drawer 视觉。

### `wbtRenderBackfillRuleState`

用于：

- 已选 N/5；
- 任务预估联动；
- 不再更新 `<details summary>` 文案。

### `wbtRenderBackfillJobs`

任务卡视觉改紧凑；Drawer 内最多显示最近 3 条，提供“更多任务见任务中心”。

## 13.3 新增函数

建议：

```js
wbtOpenBackfillDrawer(mode)
wbtCloseBackfillDrawer()
wbtSetBackfillMode(mode)
wbtRenderL0Kpis(rows)
wbtRenderL1Kpis()
wbtRenderL2Kpis(summary)
wbtSparklineSvg(points)
wbtFormatDate8(yyyymmdd)
wbtWeekdayCn(date)
wbtRenderCoverageWarning(j)
```

---

# 14. 后端函数级修改清单

## 14.1 `api_routes/tracking.py`

### 新增 helper

```text
_with_exec_excess_stats
```

### L0 `api_track_rules`

增加：

```text
weekly_equal_mean_excess_exec
weekly_equal_valid_weeks_excess_exec
trend_weeks
```

`trend_weeks` 最多 5 个，按时间升序返回，方便直接绘图。

### L1 `api_track_rule_weeks`

`aggregate` 返回前补 exec excess 统计。

### L2 `api_track_week_detail`

增加：

```text
ui_summary
```

并保证 rule_id canonical/兄弟 id 归并后统计不重复。

## 14.2 `api_routes/track_backfill.py`

修改全部规则耗时提示，不再声称“1~3分钟”。

业务逻辑、护栏、MAX_SUBSET_RULES=5 不动。

## 14.3 `service/screen_tracking.py`

本轮原则上不需改 schema。

除非测试发现历史产物缺 `excess_exec`；如果确实有旧产物没有该字段，应只做兼容 null，不允许 UI 假算 benchmark。

## 14.4 `service/track_export.py`

UI 改成 exec 超额后，导出必须同步，避免 UI/XLSX 对不上。

建议：

### 指标汇总

保留旧信号口径字段但显式改名：

```text
近N周平均收益(信号收盘)%
近N周平均收益(首日开盘)%
近N周平均超额(信号收盘)%
近N周平均超额(首日开盘)%   ← 新增
```

### 周汇总

新增：

```text
平均超额(首日开盘)%
```

### 周明细

已经有 `excess_exec`，保留。

不要悄悄用 exec 覆盖原 sig 列名，必须做到列名和算法一致。

---

# 15. 数据口径完整定义（给编码 AI，禁止自行猜）

## 15.1 主 UI 入场口径

```text
跟踪周首个实际交易日开盘价
```

对应：

```text
entry_open_week
ret_close_exec
win_rate_exec
mean_ret_close_exec
bench_ret_exec
excess_exec
max_gain_exec
max_gain_exec_date
```

## 15.2 期末

```text
跟踪自然周最后一个实际交易日收盘
```

对应：

```text
close_week_end
```

## 15.3 成交性

`exec` 统计默认排除：

- `limit_up_unbuyable`
- `no_bar`
- `unknown`

这些样本不能因为有价格就被强行塞回胜率分母。

## 15.4 benchmark

当前固定：

```text
沪深300 / SSE.IDX.000300
```

UI 不提供选择器。

## 15.5 超额

```text
excess_exec = ret_close_exec - bench_ret_exec
```

benchmark 不完整时：

```text
excess_exec = null
```

不能按 0 benchmark 代替。

---

# 16. Loading / Empty / Error / Pending 状态

## 16.1 Loading

不要白屏。

建议：

```text
正在加载跟踪数据…
```

KPI 可用 skeleton 或轻占位，但不引入 skeleton 库。

## 16.2 Empty

L0：

```text
暂无跟踪数据
周五链首次运行后自动生成。
```

L1：

```text
该指标暂无历史跟踪周。
```

L2：

```text
该周暂无可展示的个股跟踪结果。
```

## 16.3 Error

只在对应局部区域显示，不把整个 view 变红。

## 16.4 Pending

L1：状态“待结算”。

L2：

```text
本周跟踪窗口尚未结束，以下数据会随交易日推进更新。
```

如果没有产物但有 `pending_picks`，展示名单，不伪造收益。

---

# 17. Backfill 特殊语义

## 17.1 回填完整提示

必须继续用后端 `BACKFILL_NOTICE` 单一文案源。

## 17.2 subset

`rules_scope=subset` 时：

- L0：规则行轻提示；
- L1：该周来源 tag；
- L2：显眼 `scope_notice`；
- 导出 meta 继续标明。

## 17.3 已有发布快照

指定规则补算被后端拒绝时，直接显示后端清晰文案，不要把 400 转成“补算失败”。

---

# 18. 性能要求

## 18.1 L0

禁止 N+1：

```text
9 个策略 ≠ 1 + 9 个请求
```

Sparkline 点直接由 L0 API 返回。

## 18.2 L2

行展开必须本地渲染，不重新请求后端。

## 18.3 Drawer

规则目录只在首次需要时加载；加载过后复用 `bfRulesLoaded`。

## 18.4 轮询

- Drawer 关闭不代表必须停止后台任务；
- 离开跟踪一级栏目时停止 Web 轮询；
- 回来后调用 status 一次恢复。

---

# 19. 可访问性与键盘

最低要求：

- Drawer `role="dialog"`；
- `aria-modal="true"`；
- close 按钮有 `aria-label="关闭历史补算"`；
- ESC 关闭；
- 搜索输入有 label/aria-label；
- 可点击表头保留 title；
- `详情` button 的 `aria-expanded` 随状态更新；
- 收益颜色不能是唯一信息渠道，正负号必须保留。

---

# 20. 响应式

本系统主要是桌面研究工作台。

## >= 1440

严格接近效果图。

## 1200~1439

- KPI 仍 4/5 列；
- 表格允许横向滚动；
- Drawer 560px 左右。

## < 1200

不追求 1:1，但不能坏：

- KPI 2 列；
- Drawer `width:min(92vw,590px)`；
- 表格横向滚动。

手机端不是本轮验收阻塞项。

---

# 21. 测试整改方案

当前 `tests/apps/astock/test_track_ui.py` 对旧 DOM 有大量静态断言。改 UI 时必须同步改测试，不能为了过旧测试保留错误结构。

## 21.1 必改旧断言

删除/重写：

- “必须 `<details id=wbTrackBfBox>`”；
- “补算入口默认折叠”；
- “周一开盘价”；
- “周五收盘价”；
- L0 `excess` 必须是 `weekly_equal_mean_excess_sig` 的旧断言；
- 详情默认显示 `ret_vs_signal_close` 的旧断言（如有）。

## 21.2 新增前端测试

### Drawer

断言：

```text
wbTrackBfDrawer
wbTrackBfMask
wbTrackBfOpen
wbTrackBfClose
wbTrackBfTabSingle
wbTrackBfTabBatch
```

并断言：

- ESC handler；
- mode switch；
- 最多 5 条规则；
- batch 不发送 rule_ids。

### L0

断言：

- 4 KPI 容器；
- `weekly_equal_mean_excess_exec`；
- `trend_weeks`；
- sparkline helper；
- 不存在口径切换控件；
- 不存在 benchmark select。

### L1

断言：

- `valid_exec_count`；
- `win_rate_exec`；
- `mean_ret_close_exec`；
- `mean_excess_exec`；
- run_kind 和 rules_scope 均被渲染。

### L2

断言：

- `entry_open_week`；
- `close_week_end`；
- `max_gain_exec`；
- `ret_close_exec`；
- `excess_exec`；
- `ret_vs_week_open`；
- `track_week_dates`；
- `backfill_notice`；
- `scope_notice`；
- coverage warning。

## 21.3 后端测试

文件建议：

- `test_track_routes.py`
- `test_track_ui.py`
- `test_track_export.py`
- 必要时新增 `test_track_ui_metrics.py`

验证：

1. L0 exec excess 均值正确；
2. L1 per-week exec excess 正确；
3. L2 ui_summary 分母排除 unbuyable/no_bar/unknown；
4. benchmark null 时 mean_excess_exec=null；
5. 空仓时 win rate=null；
6. trend_weeks 最多 5 个且按周序；
7. export exec 超额与 UI API 同源。

---

# 22. 实施阶段（本地 AI 必须按阶段执行）

不要一次性让 AI 改 16000 行 HTML 后再统一排错。

## M0：创建保护点

1. 确认分支：`feature/auto-screen-track`；
2. 记录 commit：`ee3999...`；
3. 运行跟踪相关测试，保存 baseline；
4. 不先改视觉。

## M1：统计口径 P0

修改：

- `tracking.py`
- `track_export.py`
- 后端 tests

完成：

- exec excess 聚合；
- L0 trend_weeks；
- L2 ui_summary。

验收后再进 UI。

## M2：公共视觉 Token + Drawer Shell（先验证无 Sidebar）

只做：

- `.wbt-*` 基础 CSS；
- 明确 `.wb-root`/`view-track` 无左侧 Sidebar；
- Drawer 打开/关闭；
- 不改 L1/L2。

检查：JS syntax + UI tests。

## M3：L0

完成 KPI、toolbar、table、sparkline。

视觉比对图 1。

## M4：Backfill Drawer 内容

把原控件迁入 Drawer；保留 API 和 state 逻辑。

视觉比对图 2。

## M5：L1

五 KPI + 周列表 + 本地分页。

视觉比对图 3。

## M6：L2

五 KPI + warning + 新表头 + 行展开。

视觉比对图 4。

## M7：导出/任务中心/回归

验证：

- 跟踪导出；
- task center；
- 查卦象跳转；
- URL `?module=track`；
- 离开页面停止 poll；
- 返回保持层级。

## M8：像素级微调

在 **1708×921** viewport 下截图逐张对比（V1.1 主验收尺寸）：

- 页面边距；
- KPI 高度；
- 表头高度；
- 行高；
- Drawer 宽；
- 字体；
- border；
- 正负色；
- 按钮尺寸。

只做 CSS 微调，不在 M8 再改业务逻辑。

---

# 23. 1:1 视觉验收 Checklist

## L0

- [ ] **没有任何左侧纵向 Sidebar；内容区从现有页面左边距开始。**
- [ ] 1708px 下 4 个 KPI 同一行。
- [ ] KPI 卡高度、间距接近图 1。
- [ ] 搜索 + 历史补算 + 导出在右侧同一工具行。
- [ ] 主表一屏显示约 8~9 行。
- [ ] 策略名为两级文本。
- [ ] 红涨绿跌。
- [ ] 近 5 周趋势为迷你折线，不是大图表。
- [ ] 没有收益口径下拉。
- [ ] 没有 benchmark 下拉。

## Drawer

- [ ] 顶部全局导航仍可见；Drawer 从导航下缘开始。
- [ ] 右侧约 30%～33% 屏。
- [ ] 左侧内容明显遮罩变暗。
- [ ] 顶部 2 tab。
- [ ] 说明框常驻。
- [ ] 规则列表可搜索/多选。
- [ ] 任务预估是独立深色卡片。
- [ ] 底部按钮 sticky。
- [ ] ESC / mask / × 均可关闭。

## L1

- [ ] 没有左侧 Sidebar。
- [ ] 顶部返回。
- [ ] 标题 + fingerprint tag。
- [ ] 5 KPI 单行。
- [ ] 周列表是视觉主体。
- [ ] 正负收益上色。
- [ ] 回填/subset 标签分开。
- [ ] 底部有“已结算≠完整”的说明。

## L2

- [ ] 没有左侧 Sidebar；详情为表格行内展开。
- [ ] 标题包含策略名 + 信号周。
- [ ] 5 KPI 单行。
- [ ] coverage 黄色警示条明显但不抢主标题。
- [ ] 表列使用“首日开盘/期末收盘”。
- [ ] 行展开占整表宽度。
- [ ] 展开左关键指标、右实际交易日收益。
- [ ] 每日收益基于 `ret_vs_week_open`。
- [ ] 短周不出现不存在的交易日。

---

# 24. 功能验收 Checklist

- [ ] L0 点击“查看详情”进入正确 L1。
- [ ] L0 点击最新周入选数量可直达正确 L2。
- [ ] L1 返回 L0 正常。
- [ ] L1 周明细进入正确 L2。
- [ ] L2 返回 L1 正常。
- [ ] 搜索支持后端 `rule_name`，不是只依赖 `wbs.rules`。
- [ ] null 显示 `—`。
- [ ] 样本不足仍有提示。
- [ ] `complete` 不冒充数据 100% 完整。
- [ ] exec 超额和 exec 收益同口径。
- [ ] benchmark 缺失时超额显示 `—`/警告，不显示 0。
- [ ] unbuyable 不进入 exec 分母。
- [ ] backfill notice 全文存在。
- [ ] subset notice 全文存在。
- [ ] 补算指定规则最多 5 条。
- [ ] 批量补算不能带 rule_ids。
- [ ] 补算任务重复提交 409 能显示明确文案。
- [ ] 信号日填错时显示后端给出的正确最后交易日提示。
- [ ] 导出结果和 UI 的 exec 口径列一致。

---

# 25. 不可回归项

本轮视觉整改不得破坏以下已经解决的问题：

1. 跟踪是一级栏目，且继续使用现有顶部主导航，不新增左侧 Sidebar；
2. `?module=track` 首屏直达可用；
3. `wbtBind` 幂等；
4. token + condVersion 竞态保护；
5. 规则中心删除同步过滤；
6. 同 fingerprint 多 rule_id 归并；
7. code_disp 只显示 6 位，但查卦象仍用完整 std_code；
8. 历史短周使用真实交易日；
9. `no_trading_week` 自愈逻辑；
10. heavy-job 锁与退避重试；
11. backfill subset 护栏；
12. 离开跟踪视图停止状态轮询；
13. L2 展开/排序不重新请求后端；
14. immutable snapshot/track product 不覆盖。

---

# 26. 建议的代码注释规范

新增 UI 代码不要写“按效果图改”“用户说好看”一类临时注释。

应写语义注释，例如：

```js
// 跟踪主 UI 固定 exec（跟踪周首个实际交易日开盘）口径；
// sig 数据仍保留在产物/导出诊断列，不在主界面切换。
```

```python
# UI 的平均超额必须与 exec 收益同口径；历史产物已有逐票 excess_exec，
# 在只读聚合层派生，避免仅为展示字段 bump immutable track schema。
```

---

# 27. 给本地 AI 的执行提示词（可直接复制）

```text
你现在需要对 Analysis-of-A-shares 项目的 feature/auto-screen-track 分支进行
“跟踪模块 V1.1 高保真 UI 重构（V1版式 + 代码契约对齐）”。

必须先完整阅读：
1. TRACK_REDESIGN_IMPLEMENTATION_PLAN_V1.1.md
2. docs/plans/auto-screen-track/contract.md
3. wtpy/apps/astock/api_routes/tracking.py
4. wtpy/apps/astock/api_routes/track_backfill.py
5. wtpy/apps/astock/service/screen_tracking.py
6. wtpy/apps/astock/service/track_export.py
7. tests/apps/astock/test_track_ui.py
8. assets/ 下四张效果图

严格遵守以下规则：
- 不引入 React/Vue/Ant Design/ECharts；
- **禁止新增左侧 Sidebar；禁止把页面改成“左导航+右内容”的后台模板；**
- 保留原生 HTML/CSS/JS；
- 不改变 snapshot / published pointer / immutable track product 的核心契约；
- 主 UI 收益统一 exec；平均超额也必须使用 excess_exec；
- 不新增收益口径下拉和 benchmark 下拉；
- 历史补算必须改为右侧 Drawer；
- 指定规则最多5条；批量模式不支持指定规则；
- “周一/周五”正式 UI 改为“首日/期末”，逐日区按真实 track_week_dates；
- backfill_notice / scope_notice 必须保留；
- complete 不等于 coverage 100%；
- null 不得当 0；
- 先修统计口径再做视觉；
- 按文档 M0~M8 分阶段执行，每阶段都运行相关测试；
- 不要一次性大改整个 index_v3.html；
- 每阶段完成后列出修改文件、关键函数、测试结果和与效果图差异。

最终验收必须在 **1708×921 viewport** 下分别截图 L0、Drawer、L1、L2，
与四张 reference 图逐项比对，并以业务契约正确为前提做到高保真复原。
```

---

# 28. 最终交付定义（Definition of Done）

只有同时满足以下四类条件才算完成：

### A. 视觉

四张页面在 1708×921 下整体布局、比例、层级、间距、色彩、表格密度达到 V1.1 图的高保真效果；全程不出现左侧纵向 Sidebar。

### B. 业务语义

- 收益、胜率、超额主显示统一 exec；
- benchmark 固定沪深300；
- 短周语义正确；
- 分母正确；
- backfill/subset 说明正确。

### C. 稳定性

原有跟踪、补算、导出、任务中心、查卦象跳转、异步竞态保护、heavy-job 机制不回归。

### D. 自动化

跟踪相关单元/集成/源码断言全部通过；因 UI 结构变更造成的旧断言必须按新设计重写，而不是通过保留废弃 DOM 来“骗过测试”。

---

# 29. 关键决策摘要

如果本地 AI 只记住 10 条，必须记住：

1. **V1.1 图保持 V1 版式，只决定视觉，不决定示例数字；禁止新增左侧 Sidebar。**
2. **L0/L1/L2 主收益统一首日开盘 exec。**
3. **平均超额必须补 exec 聚合，不能继续用 sig。**
4. **不做收益口径下拉。**
5. **不做 benchmark 下拉，固定沪深300。**
6. **历史补算改右侧 Drawer。**
7. **Drawer 只有“指定历史周”和“批量最近N周”两模式。**
8. **周一/周五改首日/期末，逐日按真实交易日。**
9. **优先只读层派生新 UI 指标，不因 UI 改造 bump immutable product schema。**
10. **改 UI 的同时必须改旧测试断言，不能保留旧结构。**

---

# 30. V1.1 视觉禁止项（本地 AI 必须逐条检查）

本轮完成前必须确认以下情况全部为 **False**：

- [ ] 页面出现左侧纵向 Sidebar；
- [ ] 顶部导航被重做成两套导航；
- [ ] L0 右侧常驻说明栏挤压主表；
- [ ] L1/L2 改成新路由或独立站点布局；
- [ ] L2 详情改为 Modal / 右侧 Drawer；
- [ ] 历史补算继续使用 `<details>` 作为主交互；
- [ ] 收益口径出现下拉；
- [ ] benchmark 出现下拉；
- [ ] 固定写“周一开盘/周五收盘”；
- [ ] 使用绿色表示正收益；
- [ ] 把 null 显示为 0；
- [ ] 把 `complete` 写成“数据完整”；
- [ ] 把 `backfill` 写成“当时真实名单”；
- [ ] 把图片里的假版本号/股票/日期写死进代码；
- [ ] 为了 Sparkline 产生 1+N 请求；
- [ ] 为了 UI 改造覆盖 immutable track product。

# 31. 建议的最终项目目录

```text
docs/plans/auto-screen-track/ui-redesign-v1.1/
├── TRACK_REDESIGN_IMPLEMENTATION_PLAN_V1.1.md
└── assets/
    ├── 01_track_l0_overview_v1_1.png
    ├── 02_track_backfill_drawer_v1_1.png
    ├── 03_track_l1_history_v1_1.png
    └── 04_track_l2_detail_v1_1.png
```

旧的 V1 文档可以保留用于追溯，但本轮编码以 **V1.1** 为唯一实施方案。

---

**文档结束。**
