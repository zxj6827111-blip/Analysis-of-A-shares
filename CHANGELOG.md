# 更新日志 (Changelog)

本文件记录每个版本的变更内容。**每次版本更新必须在此记录，
并在 GitHub Releases 中发布对应的版本说明。**

版本号规则：
- 语义版本号维护在 `wtpy/apps/astock/version.py` 的 `APP_VERSION`
- 每次发版递增版本号（如 2.0 → 2.1 → 2.2），并打 `v{版本号}` 的 git tag
- 提交后右上角版本号自动显示新版本

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
