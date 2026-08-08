# DeepSeek 代码实施提示词：A 股行情管线 Tushare-only 最小改造

你是一名资深 Python 数据工程师、量化数据平台架构师和代码维护者。请直接在以下仓库中实施代码修改：

```text
E:\Software Development\wtpy-master
```

完整方案文档：

```text
docs/architecture/tushare_only_market_data_migration_plan.md
```

请先完整阅读方案文档，再阅读实际代码和当前 Git diff。不要只根据本提示词猜测实现。

---

## 一、任务目标

以最小代价把系统正式运行链统一为 Tushare：

1. 外部行情供应商只使用 Tushare。
2. 正式 L2 保留角色 `internal/composite_none`。
3. 正式 L1 保留角色 `internal/composite_tushare_factor_qfq`。
4. L2 的 base 改为完整 `tushare/none`。
5. L2 的 supplement 改为 Tushare 退市股票中 base 缺失的 symbol complement。
6. L1 继续使用 `composite_none × tushare adj_factor` 时点锚定派生。
7. Quick、Bagua、回测默认使用同一组正式 L1/L2。
8. TDX、tdx_local、local_vendor 退出默认同步和默认选面。
9. 保留旧 provider、manifest、blob 和明确旧 dataset ID 的历史可复现能力。
10. 用户升级后只需更新代码、重启并继续原有 Tushare 增量任务。

---

## 二、部署和兼容约束

禁止要求用户：

- 修改 `MARKET_DATA_ROOT`。
- 迁移外挂数据盘。
- 修改现有 Tushare Token 配置。
- 新建 Redis、Celery 或外部数据库。
- 安装新的第三方依赖。
- 手工输入父 dataset ID。
- 重新下载全市场完整历史。
- 修改朋友服务器现有定时任务。

必须复用：

- 现有 manifests/blobs/universes/sync_logs 结构。
- 现有 ready Tushare 数据集。
- 现有内容寻址 blob。
- 现有 DatasetStore。
- 现有 FastAPI 服务启动方式。

第一次升级允许自动生成新的 internal composite manifest 和 QFQ blob，但不得把它实现成全历史网络重拉。

---

## 三、当前已确认问题

用户曾发现 Quick 查询长期停在 `20260717`，虽然每晚同步显示成功。

根因：

1. local_vendor 上游停供，且新发布面反而从 7/24 回退到 7/17。
2. Tushare 接口单次 6000 条截断，旧同步曾生成残缺历史。
3. 查询 `_score` 以前优先 local_vendor，压过数据新鲜度。

当前已经完成、禁止回退的修复：

1. Tushare 四类历史拉取按三年窗口分页、排序、去重。
2. `_infer_incremental_resume()` 自动从最新完整父面续拉。
3. 增量窗口与旧完整历史合并。
4. UI derive 自动解析父数据集。
5. `asof=None` 查询优先新鲜度。

当前工作树存在未提交修改。开始前必须执行并阅读：

```powershell
git status --short --branch
git diff -- scripts/sync_market_data.py
git diff -- wtpy/apps/astock/data/providers/tushare.py
git diff -- wtpy/apps/astock/service/bagua_query.py
git diff -- wtpy/apps/astock/api_routes/system.py
```

不得运行 `git reset --hard`、`git checkout --`，不得覆盖或回退现有用户修改。

---

## 四、需要实施的核心能力

### 1. Tushare 产品协调器

在 `wtpy/apps/astock/data/` 下增加一个小型共享模块，例如：

```text
tushare_product.py
```

不要把所有逻辑堆在 API route 或 CLI 脚本中。

建议提供：

```python
def reconcile_tushare_product_datasets(...) -> ProductReconcileResult:
    ...

def resolve_active_tushare_product_pair(...) -> ProductPair:
    ...

def build_delisted_missing_complement(...) -> DatasetManifest:
    ...

def validate_tushare_product_pair(...) -> ValidationResult:
    ...
```

具体命名可以适配现有代码风格，但职责必须清晰。

协调顺序：

```text
最新完整 tushare/none
→ Tushare delisted missing complement
→ internal/composite_none
→ 最新 ready tushare/adj_factor
→ internal/composite_tushare_factor_qfq
→ lineage/quality/freshness 校验
```

必须幂等：

- 父 manifest ID/hash/cutoff 未变化时不重复发布。
- 返回结构化 `up_to_date`。
- 父数据缺失时返回 `waiting_for_parent`，不得发布半成品正式面。

### 2. Survivorship-safe L2

正式 L2 不能只包含当前上市股票。

规则：

```text
base = 最新完整 tushare/none
supplement = Tushare delisted symbols - base symbols
```

要求：

- supplement 与 base 严格无重叠。
- 按整只股票合并，不跨数据集拼接单只股票日期区间。
- 复用已有 blob，不复制 raw 行情。
- `build_composite_none()` 保留严格重叠校验。
- 去掉代码和 warning 中 local_vendor 的硬编码。

新 manifest metadata 至少包含：

```json
{
  "data_policy": "tushare_only_v1",
  "survivorship_policy": "listed_plus_delisted",
  "base_source": "tushare",
  "supplement_source": "tushare",
  "supplement_rule": "missing_symbols_only",
  "quality_status": "passed"
}
```

### 3. 原子 L1/L2 产品对

不要新增复杂数据库或配置。

正式产品对解析规则：

1. 找到最新 ready、`data_policy=tushare_only_v1` 的 L1。
2. 从 L1 raw parent 得到 L2。
3. 校验 L2 ready、adjustment=none。
4. 校验 L2 base/supplement 都来自 Tushare。
5. 校验 L1 factor parent 是 Tushare adj_factor。
6. 只有整组通过才成为默认产品。

L2 已生成但 L1 失败时，不得让默认回测切换到不一致版本。

### 4. 增量任务自动协调

修改：

```text
scripts/sync_market_data.py
wtpy/apps/astock/api_routes/system.py
```

行为：

- `task=tushare`：增量同步后自动 reconcile。
- `task=factor`：factor 增量后自动 reconcile。
- `task=derive`：自动解析父集并 reconcile。
- `source=all`：只运行 Tushare，不初始化 TDX/local_vendor provider。
- `task=tdx`：兼容返回 `skipped/disabled_by_policy`，不访问 TDX。

FastAPI 启动时只做轻量本地协调检查，不进行数小时的网络全量下载。

### 5. 查询和回测统一选面

重点文件：

```text
wtpy/apps/astock/data/dataset_binding.py
wtpy/apps/astock/service/baseline.py
wtpy/apps/astock/service/bagua_query.py
wtpy/apps/astock/api_routes/bagua.py
wtpy/apps/astock/api_routes/backtests.py
wtpy/apps/astock/api_routes/experiments.py
```

默认角色：

```text
Quick/raw    → 正式 L2
Bagua raw    → 正式 L2
Bagua QFQ    → 正式 L1
Backtest L2  → 正式 L2
Backtest L1  → 正式 L1
```

产品尚未生成时：

- Quick/raw 可以临时读取最新完整 `tushare/none`。
- 必须返回 `bootstrap_fallback=true`。
- 正式回测不得使用 survivorship-unsafe fallback。

`tdx_front`：

- 从 UI 和默认值移除。
- 显式请求返回清晰 disabled/deprecated 错误。
- 不得静默映射为 Tushare QFQ。

### 6. `_score` 和孤儿面治理

候选必须先通过：

- ready。
- blob 存在。
- symbol 日期覆盖请求。
- 策略匹配。
- 质量通过。
- 非短窗口孤儿面。

最新查询在同角色内优先：

```python
(
    symbol.max_date,
    manifest.data_cutoff_date,
    history_completeness,
    symbol.row_count,
    manifest.created_at,
)
```

历史 asof 保留时点语义，特别是 QFQ 应优先 cutoff 不早于 asof 且距离最近的锚定版本。

不要只依赖 `avg rows < 500`。必须从父 lineage、history_start、相对行数下降和 sync_mode 判断孤儿窗口面。

### 7. 发布质量门槛

新候选出现以下情况不得 supersede：

- 实际最大日期回退。
- symbol_count/total_rows 异常下降。
- 大量 symbol 只剩短窗口。
- history_start 大范围后移。
- 父 manifest/blob 缺失。
- supplement 重叠。
- L1/L2 lineage 不一致。
- freshness 未通过。

同步任务只有在 raw、factor、L2、L1、lineage、health 全部通过后才能报告整体 success。

### 8. 数据健康接口和 UI

增加或扩展：

```text
GET /api/v1/system/data-health
```

返回：

- expected latest completed trading date。
- Tushare raw 日期。
- factor 日期。
- delisted supplement 状态。
- 正式 L2 日期和 ID。
- 正式 L1 日期和 ID。
- L1/L2 lineage。
- trading-day lag。
- healthy/warning/stale。
- 最近同步错误。

Dashboard：

- 不再显示 `latest_local_vendor`。
- 显示真实正式 L1/L2 日期。
- 显示 Tushare raw/factor。
- 停更时明确标红。
- 移除 TDX/local_vendor 默认同步入口。

P0 不要求 Webhook 或邮件，不新增必填告警配置。

### 9. 历史回填

不要让第一次升级执行 2～6 小时的全量下载。

本次至少保证设计上区分：

- 当前最新日期增量。
- 2001 年以前历史完整性。

如实现分批回填：

- 使用 checkpoint。
- 每次固定 symbol/API 预算。
- 不阻塞日常增量发布。
- 不增加必填配置。

如果本轮不实现完整回填执行器，必须在 health/API 中明确报告历史完整性，而不能声称已经补齐。

---

## 五、重点文件

预期涉及：

```text
scripts/sync_market_data.py
wtpy/apps/astock/data/providers/tushare.py
wtpy/apps/astock/data/composite_dataset.py
wtpy/apps/astock/data/dataset_binding.py
wtpy/apps/astock/data/tushare_product.py          # 建议新增
wtpy/apps/astock/service/baseline.py
wtpy/apps/astock/service/bagua_query.py
wtpy/apps/astock/service/backtest_artifacts.py
wtpy/apps/astock/research/signal_cache.py
wtpy/apps/astock/api_routes/system.py
wtpy/apps/astock/api_routes/bagua.py
wtpy/apps/astock/api_routes/backtests.py
wtpy/apps/astock/api_routes/experiments.py
wtpy/apps/astock/api.py
wtpy/apps/astock/web/static/index_v3.html
wtpy/apps/astock/web/static/quick.html
tests/apps/astock/*
```

请以实际调用链为准，保持改动最小。不要为了“整洁”进行无关重构。

---

## 六、测试要求

先运行相关定向测试，再运行：

```powershell
python -m pytest tests/apps/astock -q
```

已知历史基线是 991 passed，但必须报告本次实际执行结果。

必须新增或修改测试覆盖：

1. `source=all` 不调用 legacy provider。
2. Tushare 增量后自动 reconcile。
3. reconcile 幂等。
4. L2 base 为 Tushare。
5. supplement 是无重叠 complement。
6. 退市股票进入正式 L2。
7. L1 raw parent 等于正式 L2。
8. 日期回退不切换。
9. 16 行孤儿面不被选中。
10. Quick 不选 legacy。
11. Bagua 默认是 Tushare QFQ。
12. `tdx_front` 返回清晰错误。
13. 旧 dataset ID 仍可读取。
14. 无新增环境变量时可启动。
15. 产品未就绪时回测不使用不安全 fallback。
16. health API 使用正式 L1/L2 日期，不使用 universe 最大日期代替。

---

## 七、运行验证

完成代码和测试后启动：

```powershell
python -m wtpy.apps.astock serve
```

检查：

```text
GET /api/v1/quick/600000
GET /api/v1/quick/000001
GET /api/v1/quick/600519
GET /api/v1/system/data-health
POST /api/v1/bagua/query
```

截至 2026-08-05，本地当前数据验收目标：

- 三只股票最新交易日为 `20260804`。
- raw 指向正式 L2 或明确标记 bootstrap fallback。
- QFQ 指向正式 L1。
- L1/L2 lineage 都是 Tushare-only。

必须进行故障测试：

- 模拟 Tushare 请求失败。
- 模拟日期回退。
- 模拟短窗口面。
- 模拟 factor 缺失。
- 模拟 QFQ 派生失败。
- 模拟 supplement 重叠。

确认旧正式产品继续可读、新产品不切换、任务不报告 success、health 明确告警。

---

## 八、禁止事项

- 不删除现有 manifests/blobs。
- 不修改外挂盘真实生产数据，除非用户明确授权。
- 不执行破坏性 Git 命令。
- 不回退当前未提交修改。
- 不新增必填环境变量。
- 不增加新依赖。
- 不把 local_vendor 改名伪装成 Tushare。
- 不用 UI 文案变化代替后端选面修复。
- 不跳过 survivorship-safe 校验。
- 不静默改变 `tdx_front` 语义。
- 不在启动阶段自动进行长时间全量下载。

---

## 九、最终交付格式

实施完成后请提供：

### 1. 变更摘要

- 修改文件列表。
- 每个文件的关键行为变化。
- 是否新增模块。

### 2. 数据和兼容性说明

- 为什么不需要迁移外挂盘。
- 为什么不需要修改配置。
- 第一次增量后如何自动生成产品面。
- 旧数据集如何继续复现。

### 3. 验证证据

- 定向测试命令和结果。
- 全量测试命令和结果。
- API 冒烟结果。
- L1/L2 dataset ID、cutoff 和 parent lineage。
- 故障注入结果。

### 4. 未完成或风险

- 未执行的真实数据验证。
- 历史回填是否只完成设计、尚未执行。
- 任何仍依赖实际服务器环境确认的事项。

### 5. 供 Codex 复核的材料

请输出：

```powershell
git status --short
git diff --stat
git diff --check
```

不要自行提交、推送或清理用户工作树，除非用户另行明确要求。

