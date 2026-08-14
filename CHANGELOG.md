# 更新日志 (Changelog)

本文件记录每个版本的变更内容。**每次版本更新必须在此记录，
并在 GitHub Releases 中发布对应的版本说明。**

版本号规则：
- 语义版本号维护在 `wtpy/apps/astock/version.py` 的 `APP_VERSION`
- 每次发版递增版本号（如 2.0 → 2.1 → 2.2），并打 `v{版本号}` 的 git tag
- 提交后右上角版本号自动显示新版本

## [2.6] - 2026-08-15

### 修复
- **股票链增量同步父数据集选择排除指数/ETF 集**：
  - 根因：`_infer_incremental_resume` 选父只用行数+符号数启发式；纯 ETF/指数数据集（`--asset-class index|etf|all` 产物）与股票共用 `tushare/none/1d` scope 且行数达标，被选为股票增量同步的父后历史合并为空（符号 IDX/ETF 与 STK 不匹配），每日增量变孤儿窗口，reconcile 的 base 检查拒绝发布 → 正式 L1/L2 冻结在最后一个全量 base（生产 2026-08-11 起，8-14 晚 raw/factor 已到 0814 但 L1/L2 停在 0810）
  - `_infer_incremental_resume` 排除 `universe_type=index_etf` 标记与纯指数/ETF 符号集，与 `_infer_index_etf_parent` 过滤完全对称
  - `_sync_dataset` 新增 `universe_type` 参数并写入 manifest；ETF 链 full/incremental 产物打 `index_etf` 标记（结构化标记，替代符号名启发式）
  - `_is_tushare_raw_base_candidate` 结构化排除 `index_etf` 标记（正式 L1/L2 base 选择双保险）
  - 新增回归测试 `tests/apps/astock/test_sync_resume_parent.py`（复现生产故障场景）

## [2.5] - 2026-08-14

### 新增
- **Tushare 成分股数据源**：指数/ETF 成分股查询改由 Tushare 提供（`tushare_constituents.py`），替代通达信本地数据；服务器无 TDX 部署可用
- **全市场导出池从数据仓库推导**：导出 ETF 池与全市场股票池统一从 raw 数据根推导；剔除仅含 ETF 的数据集
- **退市股票池自动回补与手动产品合并**：`sync_tushare_delisted.py` 增量自动回补；数据中心新增手动合并产品入口
- **Tushare 依赖正式声明**：`requirements.txt`/`setup.py` 补 `tushare>=1.4.0`（此前仅本机手动安装，服务器部署会导致链路失败）
- **服务端部署就绪性**：`deploy/install_astock.sh`、`deploy/astock.env.example`；日历缺失时 Tushare 兜底；首日全量同步与 ETF 锁

### 修复
- **Tushare bar 顺序归一**：`_sync_dataset` 落盘前按 trade_date 升序排序（Tushare daily 返回倒序，曾导致 blob 倒序、freshness 门与 QFQ 推导失效）
- **Tushare null/NaN OHLC 容错**：指数/基金日线历史空值回落 close、成交量/金额归零，不再崩溃
- **Tushare 全链节流与重试**：跨进程共享限流门（rate gate），整链滞后时退避重试
- **同步状态卡显示**：EOD 自动同步徽标与上次结果展示修复
- **正式面基线排除 ETF-only 数据集**：raw base 选择排除不含股票符号的数据集

## [2.3] - 2026-08-10

### 新增
- **EOD 自动同步可见性与失败重试**：
  - 子进程输出落盘 `sync_logs/eod_sync_<date>.log`（此前写入 DEVNULL，"同步似乎什么都没做"的根因）；watcher 线程记录退出码与结束时间
  - 失败当天以 `ASTOCK_EOD_SYNC_POLL_SECONDS`（默认 30 分钟）间隔自动重试，最多 `ASTOCK_EOD_SYNC_MAX_RETRIES`（默认 2）次；成功即清零
  - `last_trigger_date` 持久化启动恢复：重启不会重复触发当日全链，也不会丢掉失败重试资格
  - `/api/v1/eod-sync/status` 新增 `last_sync_exit_code / last_sync_finished_at / retry_count / pending_retry_at / state_suspect`；数据仓库页状态卡显示上次结果（成功/失败+已重试次数）
  - 启动时校验数据根存在：无可用数据集时醒目提示并给出体检命令，不再静默跳过
- **手动同步互斥与自动续传**：
  - tushare raw 增量加入跨进程 `SyncTaskLock` 硬互斥（EOD 自动同步 / UI 按钮 / cron 并发运行禁止重叠）
  - `data_sync_start` 检测到 EOD 子进程存活返回 409（明确提示等待结束）；反向由脚本内锁兜底
  - 检测到残留 checkpoint（中断/被杀进程遗留）自动附加 `--resume`，不再以 "sync failed" 形式失败关闭
- **CA 公司行为每日自动更新**：由「启动时 30 天检查」改为每个交易日 `ASTOCK_CA_SYNC_TIME`（默认 18:35）定时自动增量同步，日志与退出码落盘，失败次日自动重跑
- **数据中心 UI 分组**：数据卡片按「📥 从 Tushare 拉取 / 🏭 本地自动合成」分组并标注角色（正式L2=成交/估值·卦象面，正式L1=信号面）；去除编号与截止/起始日期输入（手动同步始终拉最新增量）；数据集明细表默认收起
- **新增 `scripts/check_data_root.py` 数据根体检**：检查预置数据根能否被系统直接识别（manifests/blobs/各数据面状态/滞后/完整度），退出码 0=可用、1=不可用、2=可用但不完整

### 修复
- 自动同步启动失败（Popen 异常）不再被当作"当日已完成"：写入非 0 退出码并进入重试闭环
- 服务重启后不再绕过 30 分钟重试间隔立即重试（`pending_retry_at` 未到不触发）
- EOD 与 CA 两个 watcher 线程并发写 `eod_sync_state.json` 加锁并原子替换，避免丢失更新/损坏 JSON
- 测试不再污染真实 `storage/astock/eod_sync_state.json`（新增 `ASTOCK_EOD_STATE_PATH` 隔离；曾因写入未来日期导致次日自动同步被误判"已触发"而跳过）
- 数据健康日历判断修正：仅当数据日期超过日历最后一天才判日历过期（数据未追平今天属正常态）

## [2.2] - 2026-08-10

### 新增
- **收盘后自动更新行情数据**（无需手动点击）：
  - 服务启动时后台检查数据新鲜度（raw lag），工作日到达 `ASTOCK_EOD_SYNC_TIME`（默认 18:30）后定时检查，发现滞后 ≥ `ASTOCK_EOD_SYNC_MIN_LAG_DAYS`（默认 1）个交易日自动触发 Tushare 全链增量同步（raw→factor→reconcile）
  - 调度优化：未到设定时间不空转（直接睡到 18:30 到点才检查）；数据晚出时以 `ASTOCK_EOD_SYNC_POLL_SECONDS`（默认 30 分钟）重试；每自然日最多触发一次；周末睡到周一
  - 触发记录持久化到 `storage/astock/eod_sync_state.json`；新增 `/api/v1/eod-sync/status` 接口与数据仓库页「🤖 收盘后自动更新」状态卡（开关、定时时间、上次自动同步时间、数据状态）
  - 全部可配置：`ASTOCK_EOD_SYNC_ENABLED / _STARTUP / _TIME / _MIN_LAG_DAYS / _POLL_SECONDS`（见 `.env.example`），默认开启
- **卦象导出修复与提速**：
  - 修复全市场导出后台任务卡死在 `queued` 的问题：`_bq_start_export_job` 启动工作线程时漏传 `ctx`，线程启动即抛 TypeError 崩溃，job 永不进入 running（前端永远显示「已排队」）；补回归测试
  - 日柱表（桌面 28MB xlsx）解析结果持久化到 `storage/astock/rizhu_cache.json`，冷解析 20s+ 降至毫秒级（进程重启也不再重读）
  - `BaguaPlaneSession` 构建走只读路径（`deep_copy=False`，跳过 13.8 万条 symbol 记录的 deepcopy）
  - 新增诊断接口 `/api/v1/bagua/export/jobs`（列出所有导出/同卦任务状态与进度）
  - 卦象导出调整：月卦默认取查询月份的上一个月（8月查询导出7月月卦，避免未收官月卦）；周卦维持查询周；导出表列序改为先周卦后月卦，表头标注周/月（如 周卦(2026-W33) / 月卦(2026-07)）
  - 全市场导出升级：A股与全量ETF（通达信本地目录枚举）合并为同一 Excel 的两个 sheet（stock-all / etf-all）；导出表格的卦象组合与爻辞列去除卦符字符（如 ䷉履卦 → 履卦），减小表格体积
- **页面刷新性能优化**（F5 长时间加载问题）：
  - `/api/v1/market-data/status` 冷扫描从约 13 秒降至约 0.5 秒：新增 30s 响应缓存 + blob 目录统计独立 300s 缓存（blob 目录约 13.8 万个文件，不再每次全量 stat）；`load_manifest` 新增 `deep_copy=False` 只读路径（跳过对 13.8 万条 symbol 记录的 deepcopy），`market_data_status` 与 `resolve_active_tushare_product_pair` 只读调用复用
  - `/api/v1/version` git 构建信息缓存 TTL 从 5s 提至 300s（消除 Windows 上每次刷新触发 `git status` 的 ~0.6s 阻塞）
- **同卦 / 同日柱股票检索**：查询卦象页查询单只股票后，结果下方新增「查看同卦股票」（全市场扫描主卦+动爻完全相同，384态）与「查看同日柱」（静态日柱表 code6→日柱）按钮；新增后端接口 `/api/v1/bagua/same-gua`、`/api/v1/bagua/same-rizhu`（GET/POST，支持 scope 限定股票池与 limit 截断）
  - 全市场同卦扫描耗时数分钟，`/api/v1/bagua/same-gua` POST 默认转后台任务（`/jobs/{id}` 轮询进度、`/jobs/{id}/result` 取结果），前端按钮事件改为容器委托（修复批量查询后按钮点击无响应）
  - 同卦扫描带**可视化进度条**（百分比 + 扫描 x/5217 + 成功/失败计数）；同卦/同日柱结果页与进度面板均提供「← 返回卦象查询结果」按钮，可随时回到原个股卦象界面
  - 同卦扫描任务**幂等**：同参数任务已在排队/运行时不重复创建（前端记住活跃任务直接复用轮询；后端 `/api/v1/bagua/same-gua` POST 幂等兜底，刷新页面后重复提交也返回在跑任务）

## [2.1] - 2026-08-08

### 新增
- **Tushare-only 正式产品链**：新增 `data/tushare_product.py` 产品协调器，构建 raw → factor → L2（composite_none）→ L1（composite_tushare_factor_qfq）的原子产品链
  - 正式 L1/L2 由 `resolve_active_tushare_product_pair` 原子解析，`validate_tushare_product_pair` 强制校验 lineage
  - lineage 校验 fail-closed：L1 raw/factor parent、L2 base/supplement parent 必须存在、ready、来源与 role 正确，缺失或畸形即拒绝发布
  - 默认执行面从 local_vendor/TDX 切换为正式 L2；`tdx_front` 价格面停用（返回明确 400）
- **因子增量同步优化**（`sync_market_data.py`）：
  - 按 `trade_date` 批量拉取全市场复权因子（约 6000 次 API 调用 → 约 40 次，15 分钟 → 1 分钟）
  - 增量窗口与父数据集合并（窗口值优先）、空窗口保留父 blob（`no_new_rows_parent_retained`）
  - freshness 逐股票覆盖门禁（阈值 0.95，含 3 天因子侧容差与停牌/退市豁免），不信任 manifest provenance
  - 自动 universe 生成（full-market 双条件门禁 + 来源 dataset ID/时间元数据）
  - `--keep-raw-batches N` 自动清理旧批次快照；增量固定写入 `latest/` 目录
- **失败语义与可观测性**：
  - CLI 业务退出码：0=成功 / 1=失败 / 2=warning-partial，UI 任务中心同步适配
  - raw 非 success 立即停止（不执行 factor/reconcile）；factor 非 ready 不执行 reconcile
  - 同步日志持久化 freshness 指标（ratio/stale 样例/p50/p10/gate reason）
  - dashboard L1/L2 状态卡与 active pair 原子一致；factor tile 展示最新候选（含 freshness-blocked partial）
  - data-health 报告逐股票因子新鲜度与历史完整度

### 修复
- Tushare 因子全量同步被限速拖慢、且每次全量重建历史的问题（改为窗口增量 + 批量拉取）
- 空 trade_date probe 导致全窗口空仍发布 ready 的问题（空 probe 回溯 7 天，仍无数据回退逐股）
- 小池/孤儿窗口（16 行截断）被选为 factor baseline 或 universe 来源的问题
- 旧 ready factor 绕过 freshness 门禁、残缺 L1 进入正式产品的问题（reconcile/derive 均加发布前门禁）
- 指数/ETF 卦象查询在正式产品对存在时丢失仓库数据路径的问题
- 幂等 derive 返回全零统计误导运维的问题
- 长假期后 reconcile freshness 预检查误阻塞产品发布的问题
- local_vendor 同步成功后 checkpoint 未清理导致下次运行失败的问题

### 优化
- 卦象查询 `_score` 缓存 manifest 统计（消除全市场 O(N²) 扫描）
- 跨天 resume 自动降级为 fresh 窗口（不再强制 `--fresh`）
- `fetch_adj_factor` 参数互斥校验；no_data 门槛对小 universe 设置下限（至少容忍 2 只）

## [2.0] - 2026-08-03

### 新增
- 新增对**大盘指数、ETF 指数、ETF 个股的查询卦象**支持：
  - 指数（sh000001 上证指数、sh000300 沪深300、sz399001 深证成指、sz399006 创业板指等）
    与 ETF（sh510300 沪深300ETF、sz159915 创业板ETF、sh588000 科创50ETF 等）可直接查询卦象
  - 查询指数/ETF 后可一键「查看成分股卦象」批量计算成分股
  - 指数/ETF 预设下拉（指数 9 项 + ETF 10 项），页面打开即时可用
  - Tushare 数据源新增指数/ETF 日线同步（index_daily / fund_daily）与全量/增量同步脚本
- 版本号体系：页面右上角显示版本号（V2.0），随 git 提交自动更新
- 卦象查询页新增「操作说明」面板（替代原「算法说明」），写明完整操作方法

### 修复
- 点号形式符号（如 510300.SH）被误判为股票，导致 ETF 查询失败的问题
- 多 symbol 请求下 bar 标的错乱的问题
- 指数与 ETF 增量同步共用 checkpoint 文件互相冲突的问题
- manifest 缓存返回共享可变对象、修改可能泄漏到其他调用方的问题

### 优化
- 指数/ETF 预设下拉加载提速：静态项即时渲染 + 前端缓存 + 后端 60 秒缓存
- 顶部导航精简：移除帮助/系统诊断入口，显示版本号
