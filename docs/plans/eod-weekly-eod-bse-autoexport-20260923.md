# 2026-09-23 三项功能：周最后交易日调度 / 北交所接入 / 链尾自动数据表

需求（用户原话归纳）：
1. 「数据更新时间的，现在是每周五更新，因为涉及到节假日的问题……要修改成每周按照最后一个交易日来更新」——中秋 2026-09-25（周五）休市，数据更新应提前到 9-24（周四）。
2. 「数据的话需要帮我支持北交所的。」
3. 「更新完成之后，马上就帮我自动生成一份可下载的表格……包含大盘指数、ETF、所有A股（指标筛选出来那几张 sheet 之外的内容）。」

## 设计决策

### 1. 调度口径：last_trading_day（默认）

- 判定依据：Tushare `trade_cal(exchange=SSE)` 前瞻日历——交易所提前发布全年开/休市，
  几千次实测（2026-09-23）确认能覆盖到当年底（含中秋/国庆/春节）。沪深北节假日一致，
  SSE 日历即全市场口径。
- 缓存：`storage/astock/trade_cal_forward.json`（schema v1），`service/eod_schedule.py`
  惰性刷新（≥7 天或覆盖不到下周日才联网）。
- **兜底**：日历缺失/陈旧/拉不到 → 自动退化为固定周几（`ASTOCK_EOD_SYNC_WEEKDAY`，
  默认周五），并照常打印退化原因。绝不静默停更。
- 整周全周休市（春节周）当周自然无触发日，跳过；节日最后交易日即抓日。
- 调度线程变化：last_trading_day 模式下周一~周五每天在 sync_time 醒一次做日历判定
  （本地 JSON 读取，成本极低），只有判定为「今日 = 本周最后交易日」才跑全链路
  lag 健康检查。
- 既有语义全部保留：同日重试（失败按 poll_seconds 有界退避）、重启幂等
  （`last_trigger_date`）、min_lag 门。
- 周五链下游（review-weekly / track-weekly --previous-week）本来就以「自然周最后
  交易日」为 week_id（`screen_contract.natural_week_window`），口径天然一致。

### 2. 北交所（BSE）支持

- 数据层本就兼容：repository 符号变体、pit_universe 92 迁移段别名、classify_instrument
  的 BSE 分支、`is_bse_code`。缺口在两层：入口开关 + **overlay 票池可见性**（复核轮发现，
  不补的话北交所数据入库了但要等下次 consolidation 才入导出池）。
补法：链尾发布后写 `eod_universe_latest.json` 现役名单 →
`_overlay_eod_universe`（`backtest_universe`）优先读它；`BaguaPlaneSession`
对 overlay manifest 把 `OverlayView.delta_only_symbols()` 合成记录纳入索引。
- **EOD delta 链开启「新票发现」**（`--include-bse`，默认开）：
  - raw 段：现行上市名单（含 BSE）与 overlay 池（base 票 ⨿ delta 票）做差；
    差集 = 新上市票（首批北交所约 346 只）→ **逐票拉全历史**（不是 20 天窗口），
    与老票的窗口更正在同一 delta batch 提交。
  - factor 段：新票 pool 还未发布（水线没动），从 raw 段结果带回，逐票拉全历史
    `adj_factor`，同批 commit + 原子发布。
  - consolidation（govern_market_data --maintain）本来就以 `pool_symbols()`
    = base ∪ delta 收敛新票进 base——无需额外手工迁移。
  - 名单/行情/provider 任一步失败 → 回退 base 票池（增量照常跑），不因元数据停更。
- 非 delta 旧链：`sync_tushare_incremental` 补 `--include-bse` 透传（与 full 对齐）。
- 名称/上市日期兜底：`bagua_query._fetch_symbol_meta_from_tushare` 改
  `fetch_universe(include_bse=True)`——日柱补齐（list_date 推六十甲子）与跟踪页名称
  兜底自动覆盖北交所。
- 涨跌幅：`DefaultAShareLimitRule` BSE 段（43/83/87/920）= 30%，无 ST 5% 特例；
  `infer_board` 返回 `bse`。

### 3. 链尾自动生成「全市场数据表」

- 新 CLI：`python -m wtpy.apps.astock export-weekly [--date YYYYMMDD] [--keep N]`。
- 内容 = 大盘指数（index-all）+ ETF（etf-all）+ 所有 A 股（stock-all，含 BSE），
  **不含指标筛选 sheet**（显式 `review_rules=[]`，不复用「默认读周五链复核 JSON」）。
- 产物：`storage/astock/bagua_exports/auto_weekly_<date>_<stamp>.xlsx`（独立前缀，
  清理只动它，保留最近 `ASTOCK_AUTO_EXPORT_KEEP=4` 份）。
- 状态：`storage/astock/auto_export_state.json`（原子写）→ 前端
  `GET /api/v1/bagua/export/auto/latest` 拿状态、`/download` 直下（重启后仍可用；
  路径越界 403）。
- 重任务互斥：`run_with_heavy_lock`（契约 §7），抢锁失败记待办，服务运行期
  5/15/30 分钟退避自动补跑（`api._heavy_job_command` 新增 `auto_export_*` 映射）。
- EOD 链尾在 `stock_rc == 0` 时串行触发该 CLI（可选关 `ASTOCK_EOD_AUTO_EXPORT_ENABLED=0`）；
  自动导出失败不影响行情主链成败记账（附属产物）。
- UI：数据页新增「📊 全市场数据表」卡片（状态/时间/下载按钮），EOD 卡片的
  「每日定时」文案改为后端下发的 `schedule_text`（每周最后一个交易日 18:30）。

## 验证

- 新增测试：`test_eod_last_trading_day.py`（17）、`test_bse_support.py`（5）、
  `test_auto_export.py`（5）、`test_delta_chain_bse.py`（3）；更新 EOD 链编排断言。
- 真实日历冒烟（2026-09-23）：9/24（周四，中秋前最后交易日）触发、9/25 不触发、
  9/30（周三，国庆前）触发。
- 真实数据根（E:\AStockData\datasets\market_data）跑通 `export-weekly` 生成全量表。

## 部署/升级要点

- 服务器（腾讯云 VM `/opt/wtpy/wtpy-master`，venv Python 3.10）：
  `git fetch --tags && git checkout v3.2.0 && systemctl restart astock-serve`。
  无新增 pip 依赖（tushare/openpyxl 已在依赖内）。
- 首次到日的 EOD 链会自动把北交所（约 346 只）逐票拉全历史入库（raw + 复权因子），
  之后每周增量照常；无需手工回填。
- 日历需要联网的最小窗口：每周 EOD 前若缓存过期会自动刷新；服务器无外网时自动退
  化为固定周五并在日志留 `[EOD_SYNC] 前瞻交易日历刷新跳过`。
