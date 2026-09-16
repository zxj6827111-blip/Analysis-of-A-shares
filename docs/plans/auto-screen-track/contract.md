# 自动预筛选 + 入选跟踪 · 数据契约（v4.1 定稿）

> 权威实现：`wtpy/apps/astock/service/screen_contract.py`（常量与算法以代码为准，本文是语义说明）。
> 方案基线：v4.1（三轮评审通过，2026-09）。阶段 0 交付物之一。
> 本契约先于一切功能代码定稿；阶段 1/2/3 的服务层必须 import 本模块的枚举与函数，不得自造同义词。

## 0. 身份三元组

| 身份 | 定义 | 规则 |
|---|---|---|
| `snapshot_id` | 独立运行 ID：`{asof}_{毫秒时间戳}_{pid}_{随机后缀}` | **O_EXCL 独占创建**，已存在即抛错，绝不覆盖既有文件。`generated_at` 不参与唯一性保证（同精度时间可重复）。 |
| `week_id` | 信号自然周的**最后一个交易日**（YYYYMMDD），即信号日本身 | 整周休市的信号周**不产生** week_id，绝不用前一周最后交易日顶替（防同一份名单被重复发布/统计）。 |
| `published_snapshot_id` | 周索引 `screen_snapshots/index.json` 中该 week_id 的发布指针 | **统计/汇总/跟踪只认指针**。后台重试、已有指针时的 backfill、recompute 一律不自动替换。 |

- `content_fingerprint`（sha16：asof+规则集指纹+universe_fp+名称快照+data_version）**另存**于快照内，仅用于"同内容重跑"的幂等提示，不参与文件身份。
- 快照目录：`storage/astock/screen_snapshots/snap_{snapshot_id}.json`；同 asof 重算产生**新文件**，旧文件字节不动。
- 发布顺序（双写不原子，以索引为唯一事实源）：① O_EXCL 建快照 → ② 校验（结构/指纹/门槛） → ③ 锁内原子更新 index.json 指针 → ④ 由快照生成/修复 `review_{asof}.json`（导出兼容层）。中途崩溃 = 未发布 = 启动补偿重跑；review 与快照不一致以快照为准。

## 1. 状态枚举

- **run_kind** ∈ `weekly_chain` / `backfill` / `recompute`
- **规则级** ∈ `ok` / `partial`（部分票失败，hit/miss 可用但非全量）/ `error`
- **逐票**（规则×股票）∈ `hit` / `miss`（评估完成未命中）/ `error`（该规则对该票失败）/ `no_data`（asof 当日无 K 线）/ `not_in_universe`（picked 越界）
- **跟踪完成状态**（运行状态，存任务状态记录，不存不可变产物）：
  - `complete`：本轮评估已结束。**只表示评估结束，不表示数据完整**（覆盖率按口径分别记录，UI 依覆盖率警示）。
  - `pending`：窗口未结束或数据未齐 → 到期重试，不是 missing。
  - `blocked_benchmark`：股票收益已完成、基准缺 → 只补算基准+超额，产生新 revision。
  - `no_trading_week`：跟踪周整周休市 → 合法终态，**退出码 0**，不顺延不重试。
  - `failed`：计算异常 → 有界重试（当日 ≤2 次）。
  - `data_version_changed`：固定读版本前后复查不一致 → 中止本次，重试一次。
- **成交性** ∈ `ok` / `limit_up_unbuyable`（首日一字涨停，近似判定）/ `no_bar`（停牌**或缺数据**，如实标注不区分）/ `unknown`（超出 limit_rules 边界或元数据不足）——**绝不默认"正常"**。
- **退出码**：0 = complete（含合法排除样本）或 no_trading_week；3 = pending / blocked_benchmark / data_version_changed；其他非零 = failed。**有 unbuyable 票不构成任务失败**。

## 2. 自然周算法

- 信号日 = 信号 ISO 自然周内的**最后一个交易日**；若同周存在更晚交易日而传入非最后日 → anomaly（fail-closed，不静默换锚）。
- 跟踪窗口 = **下一个 ISO 自然周** civil 区间内的交易日序列（`natural_week_window`）。
- 整周休市（如春节整周）→ `no_trading_week`，不自动顺延到下一周，不借用相邻周。
- 个股端点缺行情 → `missing` / `pending`（按窗口是否结束+数据是否就绪区分），**不私自延长窗口**。
- 跨月/跨年周由 ISO 周历自然处理。
- 短周（任一端 <5 个交易日）打 `short_week` 标记。
- 存储一律用实际日期；「周一开盘/周五收盘」仅是通常情况的展示文案。
- 本周新入选名单显示**「待下周结算」**。

## 3. 双口径与统计分母

- 口径命名：**信号收盘基准收益**（理论口径）/ **跟踪周首日开盘基准收益**（**成交假设**，不承诺可成交）。
- 逐周每规则计数：`selected_count / valid_sig_count / valid_exec_count / pending_count / missing_count / unbuyable_count / unknown_count`。
- 胜率分母 = 对应口径的 valid 数；**空仓周（selected=0）胜率 = null**（不是 0）。
- `unknown` 的分母规则：信号收盘口径价格完整即可计算（可进分母）；开盘成交假设口径**默认不进分母、收益 null**；可单独展示"理论开盘收益"列但不混入已过滤成交限制的汇总。
- 跨周聚合双提供：票次加权 + 每周等权，**默认每周等权**；等权只聚合该口径 valid>0 的周并显示**有效周数**。
- **票次加权胜率** = Σ(各周 win_rate × 该周 valid 数) / Σ(有胜率周的 valid 数)——分子必须用周胜率，**绝不用 mean_ret × 票数**（那是加权平均收益，会被误读成"胜率 1.5%"）；胜率字段恒 ∈ [0,1]。
- **「周等权胜率」全系统单一算法定义 = 各周 win_rate 的均值**（L0 API 的 `weekly_equal_win_rate_sig/exec` 与导出 xlsx 的「近N周胜率(周等权)」列必须由此同一来源产出，不允许一个入口用"正收益周占比"另算一套——同名列两算法 = UI 与导出对不上账）。
- 「近 12 周」= 最近 12 个**信号自然周**（周历定义），不是倒找 12 个有收益的周。
- 基准（沪深300）双窗口：信号收盘→窗口末收盘、首日开盘→窗口末收盘；超额**单独维护**有效样本数/有效周数，不借用收益分母；基准缺 → excess=null+原因。
- `min(low)/entry−1` 命名**「最低相对入场收益」**（可为正值，如实显示）；另提供基于逐日**收盘序列**的峰谷回撤（日 OHLC 无法确定同日高低先后，故回撤按收盘算）。
- 见顶日分布/平均回吐仅是**描述统计**（并列最高取首个日期；短周按窗内序号+星期双记）；**不可由此直接推导"应提前卖出"**，验证卖出规则需独立样本检验。
- 跨周汇总按 `(rule_id, rule_fingerprint)` 分组分段；同规则变更公式后历史段隔离。

## 4. 数据版本与复权安全

- `data_version` 从 `OverlayState` 提取（复用项目已有身份，不自造）：base/factor/supplement/delisted 的 dataset_id+manifest_sha256、delta watermark+**commit_seq**、factor watermark+commit_seq。
- 基准指数面版本单独记录 key=`benchmark`：**指数补数后股票面没变，也必须使超额重算失效**。
- **固定读版本**：一次跟踪计算启动时捕获完整 data_version，批读结束后**从权威状态重读复查**（不得读同一内存缓存对象两次）；不一致 → `data_version_changed` 中止，重试一次。
- 跟踪计算的入场价（信号日收盘/首日开盘）与跟踪周 bars **从同一数据版本重读**；快照里保存的 close 仅作原始展示，不参与收益计算（防复权基准变化产生虚假收益）。
- track 产物记录 `entry_price_used / price_plane / data_version / algo_version / schema_version`。

## 5. 跟踪产物版本化

- 不可变产物：`screen_tracking/track_{snapshot_id}_{tracking_revision_id}.json`。
- `tracking_revision_id` = sha16(snapshot_id ‖ 行情 data_version 签名 ‖ 基准 data_version 签名 ‖ algo_version ‖ schema_version)。**schema_version（结构）与 algo_version（收益算法）分离**：算法修正 bump algo_version 即新 revision，旧文件保留不覆盖。
- 同 revision 文件已存在 → **校验内容并复用，不覆盖**（pending→complete 的状态变化不产生新产物身份）。
- 运行状态（pending/failed 等）存 `state_{task_key}.json`（一任务一文件，可变）。
- 股票部分产物可先落盘；基准补齐后基准版本变化 → 新 revision。
- 当前采用版本指针：`track_{snapshot_id}.current.json`（索引指向，可变）。

## 6. 发布门槛与指针规则

- `partial` / `error` 快照：保留用于诊断，**不自动发布**。
- `no_data` 比例 ≤ 阈值（默认 5%，**可配置策略默认值，不是已验证的数据质量标准**；产物记录实际阈值、缺失数与判定结果）才可发布；不能仅靠 status=ok。
- 已有任何 published 指针 → 后台重试**不自动替换**（重试产生的新快照只并列保留）。
- backfill 仅在完全无指针时补位；已发布 backfill 换 weekly_chain / 手工 recompute 转正 → 显式 `track-publish`（source=manual，门槛同样校验，索引锁内更新并记录审计：原指针/新指针/时间/来源）。
- 产物字段：`coverage_rate`（分口径）+ 各排除计数；覆盖率低 → UI 黄色警示而非绿色"已结算"。

## 7. heavy-job 锁与补偿

- 全局锁键 `heavy_job:screen_track`（**不复用** sync_lock 的 (root,source,adjustment,period) 键；复用其字节锁实现模式）。锁由执行方自持：子进程路径由子进程申请，网页现算由 API worker 线程直接持锁——**两者必须互斥**。
- 抢锁失败 → `skipped_locked` 写**持久化待办**（`heavy_job_pending.json`，按 task_key 管理，一任务一条互不覆盖）；服务**运行期间**按有界退避重试（5/15/30 分钟，首试+3 退避共 4 次），**不依赖重启、不等到下周五**。
- **待办 key 双体系与统一清账**：锁待办按**周身份**记 `track_{week_id}`（CLI 持锁前只有 week_id）；TaskState 按**快照身份**记（`tracking_task_key`，契约 §5）。跟踪终态时**两个 key 都清**（`_clear_terminal_pending`），否则手动补跑成功清不掉锁待办、残留到退避到期后白跑一轮。待办→命令映射对参数做格式校验（`track_<8位日>` / `backfill_<N>` / `review_all_<8位日|0>`），非法键不猜命令、标欠账退出自动重试（防历史格式键每 120s 空转）。
- **单一记账方**：exit 3 的待办记账只发生在**持锁子进程内**（CLI 按真实 completion 记 reason：pending/blocked_benchmark/data_version_changed/skipped_locked）；服务端重试 runner 对 rc=3 **不补记**——补记会双倍累加 attempts（一次失败 +2、退避跳档、4 次预算减半）。
- **runner 退出码记账全覆盖**（`record_runner_exit`）：0/3 不写；1/2 标欠账（exhausted）；其余异常码（信号杀死/OOM/崩溃，Windows 下 0xC00000xx 大数）与 Popen 启动失败按**有界退避**记账——任何"不记账"路径都会让待办立即又到期，每 120s 空转且占死 max_per_pass=1 的队列头。
- **回填伞待办 re-arm**：整轮回填 exit 3（含 pending 周）时对 `backfill_{N}` 伞键重新记账——否则其 recorded_at 是旧的、立即又到期，服务端每 120s 重触发一轮全市场读；re-arm 后按退避表正常推进。
- 补偿触发 = 窗口已结束 且（无产物 ∨ 非终态 ∨ 目标 revision 不一致）。**不看"文件是否存在"**；陈旧 complete（算法/数据版本升级）也被 revision 不一致捞回。
- 重试耗尽 → 保留欠账、显示失败原因、暴露手动补跑入口。
- 补偿同时覆盖两类欠账：本周应发布无正式快照 / 应结算未完成跟踪。

## 8. 评审三点约束（2026-09 终审）

1. **覆盖率按口径分别记录**：信号收益、开盘收益、超额收益各有有效样本数和分母；应评估样本为 0 时覆盖率返回 **null**。
2. **5% 是可配置的发布策略默认值**，不是已验证的数据质量标准。产物记录实际阈值、缺失数量和判定结果，实测后再调整。
3. **持久化状态与待办按任务身份管理**：不能用一个文件中的单条状态覆盖其他周的任务；重试耗尽后保留欠账、显示失败原因和手动补跑入口。

## 9. 回填提示（全文，UI/导出必带）

> 本数据为按当前规则、当前可用股票池及历史行情重建，可能存在股票池、历史名称/ST 信息和数据修订偏差；不代表当时实际发布名单。

## 10. 产物 Schema 附录（阶段 2 实现后固化，2026-09-14）

### track 产物（`screen_tracking/track_{snapshot_id}_{tracking_revision_id}.json`）
```json
{
  "schema_version": "1",
  "snapshot_id": "...", "week_id": 20260911, "asof": 20260911,
  "completion": "complete | pending | blocked_benchmark | no_trading_week | failed | data_version_changed",
  "tracking_revision_id": "...", "algo_version": "1",
  "data_version": {...OverlayState 提取...}, "benchmark_data_version": {...},
  "coverage": {"signal_close": 0.97, "week_first_open": 0.95, "excess": 0.97, ...null 当分母为 0},
  "rows": [{
    "code": "SZSE.000001.SZ", "rule_id": "txt_...",
    "entry_close_signal": 10.5,   // 重读 qfq 信号日收盘（非快照展示价）
    "entry_open_week": 10.6,       // 重读 qfq 跟踪周首日开盘
    "fill_status": "ok | limit_up_unbuyable | no_bar | unknown",
    "daily": [{"date": 20260914, "close": ..., "ret_vs_signal_close": ...,
               "ret_vs_week_open": ..., "high": ..., "ret_high_vs_signal": ...}],
    "max_gain_sig": ..., "max_gain_sig_date": ..., "max_gain_exec": ...,
    "min_low_ret_sig": ...,       // 最低相对入场收益（可为正）
    "drawdown_close_sig": ...,    // 收盘序列峰谷回撤
    "ret_close_sig": ..., "ret_close_exec": null-if-unbuyable,
    "bench_ret_sig": ..., "bench_ret_exec": ...,
    "excess_sig": ..., "excess_exec": ...,
    "status": "ok | no_bar | pending"
  }],
  "rule_aggregates": [{
    "rule_id": "...",
    "selected_count": N, "valid_sig_count": N, "valid_exec_count": N,
    "pending_count": N, "missing_count": N, "unbuyable_count": N, "unknown_count": N,
    "win_rate_sig": ..., "win_rate_exec": ...,     // null 当分母 0
    "mean_ret_close_sig": ..., "mean_ret_close_exec": ...,
    "mean_excess_sig": ...,
    "mean_max_gain_sig": ..., "mean_giveback_sig": ...,
    "max_gain_weekday_dist": {"1": n, "2": n, ...}
  }]
}
```
- 聚合字段名以 `rule_aggregates[]` / `mean_ret_close_sig` / `mean_ret_close_exec` / `mean_excess_sig` 为准（阶段 3 读取方 `api_routes/tracking.py` 依赖这些名字）。
- 退出码映射：complete/no_trading_week→0；pending/blocked_benchmark/data_version_changed（CLI 自动重试一次后仍失败）→3；no_snapshot/配置类→2；异常→1。

### 快照产物（`screen_snapshots/snap_{snapshot_id}.json`）
结构见 `service/screen_snapshots.build_snapshot_payload`；关键：`universe_codes[]` 完整清单、逐规则 `status(ok|partial|error)` + `failed_codes[]`（逐规则不共享）、全局 `no_data_codes[]`、`rule_fingerprints{}`、`data_version{}`、`content_fingerprint`。

## 11. 实现补充（2026-09-15）

### heavy-job 锁（§7 落地实现）
- 模块：`wtpy/apps/astock/service/heavy_job.py`；锁文件 `storage/astock/.locks/heavy_job_screen_track.lock`（字节范围锁，进程退出自动释放，无陈旧锁）。
- **同线程可重入**：backfill 循环内进程内调用 `review-weekly` 不会自我阻塞；同进程**其他线程/其他进程**互斥（已测）。
- 持锁方：`track-weekly`（单周与 backfill 整轮）、`review-weekly --rules all`。锁由执行方自己持有（父进程 spawn 的子进程自己申请）。
- 抢锁失败 → `record_pending_job`（退避 5/15/30 分钟，耗尽保留欠账）+ 退出码 3；待办按 task_key 隔离（`track_<周>` / `backfill_<N>` / `review_all_<日>`）。
- 服务运行期重试：`api.py _auto_heavy_job_retry` 守护线程，每 `ASTOCK_HEAVY_JOB_RETRY_POLL_SECONDS`（默认 120s，最小 30）检查到期待办，**每轮最多补 1 个**（重任务串行，9/13 OOM 教训）；EOD 同步进行中跳过本轮；`ASTOCK_HEAVY_JOB_RETRY_ENABLED=0` 可关。
- 待办 → 命令映射 `_heavy_job_command`（纯函数，有测试锁死）；参数格式校验（`track_`/`review_all_` 后随 8 位日期、`review_all_0` 为合法的"缺省 asof"、`backfill_` 后随数字）；非法键标欠账退出自动重试（不空转、不猜命令）。
- **单一记账方（2026-09-16 二审修正）**：exit 3 由持锁子进程按真实 completion 记 `track_{week_id}` 待办（reason=真实 reason）；服务端 runner 对 rc=3 不补记（曾双倍累加 attempts）。终态清欠账同时清 `track_{week_id}` 与快照身份 key（`_clear_terminal_pending`，complete 与 no_trading_week 两条终态路径都清）。回填整轮 exit 3 → 伞键 `backfill_{N}` re-arm（旧 recorded_at 立即到期会让服务端每 120s 白跑一轮全市场读）。runner 对异常退出码（信号杀死/OOM/崩溃码）与 spawn 失败同样按有界退避记账（`record_runner_exit` / `record_runner_spawn_failure`，有测试锁死），保证任何失败都不落入"立即又到期"的空转路径。

### 跟踪结果导出（阶段 3）
- `GET /api/v1/bagua/track/export?weeks=&rule_id=&entry_asof=` → `{ok, file, path, download_url, sheets, rows_total, warnings}`；无发布周 → `{ok:false, reason:"no_published_week"}`（HTTP 200，不产空文件）。
- `GET /api/v1/bagua/track/export/download?file=` → 文件名白名单正则 + `Path.resolve()` 必须仍在导出目录内（防穿越/符号链接），不满足 400、不存在 404。
- 三个 sheet（指标汇总 / 周汇总 / 周明细）+ meta（snapshot_ids / tracking_revision_ids / data_version / coverage 分口径）；含回填周时带 §9 提示全文（单一文案来源 `BACKFILL_NOTICE`）。
- 「近N周胜率(周等权)」= 各周 win_rate_sig 均值（与 L0 API 同一口径；2026-09-16 二审修正——曾按"正收益周占比"另算一套，导致 UI 与 xlsx 同名列对不上账）。

### 前端跟踪页签（阶段 3）
- 卦象工作台第 4 个二级页签 `data-wb-mode="track"`；L0 指标总览 → L1 指标周列表 → L2 周明细三级钻取；覆盖率低黄色警示、`null` 显示「—」、回填提示框、双口径（收益与胜率可切换，超额仅信号口径）、未结算票单列、行内跳「查卦象」。
- 异步加载均带 token + 条件版本竞态防护与 try/catch。

### 真实数据基准（本机 `E:\AStockData\datasets\market_data`，2026-09-15 实测）
| 指标 | 实测 |
|---|---|
| 全规则（`--rules all`，约 19 条）全市场扫描吞吐 | 约 **1.0 秒/只**（3000/5217 只实测；首轮 08:30 起跑，被外部 50 分钟超时掐断） |
| 外推全量耗时 | **约 85–90 分钟**（5217 只 × 1.0 秒） |
| 早期方案估算 | 10–25 分钟（**显著低估，已按实测修正**） |

结论与影响：周五链 review 段是本轮最重的单段（远重于 track）；18:30 起跑、约 20:00 前后完成，夜间链可接受。若需缩短，方向是规则数裁剪或分批并行（但服务器 8 GB 有 OOM 前科，并行需先实测内存峰值）。**该数字已替换方案中"10–25 分钟"的估算，5% 发布阈值等策略值仍待多周实测后再评估。**

## 12. 规则范围快照「指定规则补算」（2026-09-16 新增）

场景：验证**某一条规则**在过去某周选出了什么、表现如何。全市场扫描成本与
规则数近似线性（本机真实数据实测：5217 只 × 12 条规则 = 0.60 s/票，其中
bars 加载 0.045 s、公式计算 0.56 s → 单条规则中位 0.052 s/票，含加载约
0.10 s/票，即**单规则约为全量的 1/6~1/10**；整周全量约 52 分钟实测 64~71
分钟，单规则约 8~11 分钟）。

### 12.1 产物与字段

- 快照新增 `rules_scope` ∈ `all | subset`（缺字段 = 旧快照 = `all`）与
  `scoped_rule_ids[]`（subset 时=实际覆盖的规则 ID；all 时空数组）。
  周索引条目冗余同名字段，读取方不加载快照即可区分。
- 子集快照的 `universe_codes` / `no_data_codes` / `missing_count` 仍是
  **全市场口径**（筛选成本与股票池无关），只有 `rules[]` 是子集。
- 唯一文案来源 `sc.subset_scope_notice()`：L2 接口与导出 meta 共用；UI
  不得另写一套措辞。文案必须说清"其他规则当周**没有名单**，不代表当周
  没有入选"——否则会被读成"这些规则当周空仓"。

### 12.2 三条硬约束（缺一即破坏既有语义）

1. **不写 `review_{asof}.json`**：那是周五链/导出侧共享的**全规则**结果。
   子集复核一律 `persist=False`（`cmd_review_weekly` 的 `--publish-scope
   subset` 分支），否则该周导出会直接错数据。
2. **发布护栏**（`publish_decision(..., rules_scope="subset")`）：
   - 该周已有任何发布指针 → `already_published`（**不替换**，含不替换另一个
     子集快照；要追加规则请一次性提交全部目标规则）；
   - 该周 ≥ 周索引中最新发布周 → `subset_scope_not_historical_week`。
     理由：最新的周归周五链，子集（**部分名单**）一旦占住发布指针，链的全量
     快照会被"已有指针不替换"永久挡住，该周就只剩这几条规则的数据。
   入口层再加一道 `latest_signal_week`（数据面最新信号周）判定用于快速失败
   （CLI `_cmd_track_backfill` 逐周跳过、API `_subset_scope_hint` 400）。
3. **读取方如实标注**：L0 行带 `subset_weeks` 计数、L1 周行与 L2 带
   `rules_scope`/`scoped_rule_ids`（L2 另带 `scope_notice`）、导出 meta 带
   `subset_scope_weeks` + 逐周 `subset_scope_notice` 并进 `warnings`。

### 12.3 入口与互斥

- CLI：`track-weekly --backfill N --rules A,B`（规则白名单 fail-closed：
  拼错的规则名拒绝启动，绝不放行成"该周什么都没有"的空名单快照）；
  单周跟踪不接受 `--rules`（只读已发布快照，规则范围由快照决定）。
- CLI：`review-weekly --rules A,B --publish-scope subset`（`--publish-scope
  all` 必须配 `--rules all`，防"部分名单当全量发布"）；`--run-kind` 缺省在
  子集模式下为 `backfill`。
- API：`POST /api/v1/bagua/track/backfill` 支持 `{week, rule_ids}`（限
  `MAX_SUBSET_RULES=5` 条；不与 `weeks_back` 同用；同周不同规则集不判重）。
- heavy-job：子集回填伞键 `backfill_subset_{N}`、子集复核待办键
  `review_subset_{asof}`——它们的重跑命令含规则清单，`_heavy_job_command`
  **不猜命令**（返回 None → 标欠账不再自动重试）。子集补算是用户交互式发起
  的一次性验证任务，锁被挡住时如实告知重跑即可；因此子集伞键也不做
  re-arm（re-arm 只会让服务端每 120 s 空转）。
- 跳过与未发布的如实上报：逐周护栏命中 → `skipped_subset_scope`；
  复核 exit 0 但被门槛/护栏拒绝发布 → `review_not_published`
  （不能退化成含糊的 `skipped`）。

### 12.4 验收实测（本机 `E:\AStockData`，20260731，5217 只）

| 项 | 实测 |
|---|---|
| bars 加载 | 0.045 s/票（8%） |
| 12 条规则公式计算 | 0.560 s/票（92%） |
| 单条规则（中位 / 最贵） | 0.052 / 0.075 s/票 |
| 整周全量（含跟踪结算） | 约 52 分钟外推；既有快照实测 3841~4244 s（64~71 分钟，含名称快照等） |
| 单规则外推（含跟踪结算） | 约 9~11 分钟 |

结论：单规则补算是"历史周单规则验证"的正确粒度；服务器 8 GB 内存下仍按
契约 §7 串行（单 worker + heavy-job 全局锁），不因规则少而放开并发。

## 13. 版本

- v4.1（2026-09）：三轮评审定稿；本文件为阶段 0 契约交付物。
- 2026-09-14：阶段 1/2/3(后端+API) 实现后补 §10 schema 附录。
- 2026-09-15：补 §11（heavy-job 锁实现、跟踪导出端点、前端页签、真实数据基准实测）。
- 2026-09-16：二审修正——§3 票次胜率算法（曾以加权收益冒充胜率）与「周等权胜率」全系统单一算法定义（§3/§11）；§7 待办 key 双体系统一清账、单一记账方、rc=3 真实 reason、伞待办 re-arm、命令映射格式校验。
- 2026-09-16：新增 §12「指定规则补算」（规则范围快照 rules_scope=subset、
  发布历史周护栏、persist=False、读取侧标注、入口与互斥、验收实测）。
