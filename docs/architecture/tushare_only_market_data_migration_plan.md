# A 股行情管线 Tushare-only 最小改造计划

文档日期：2026-08-05  
适用仓库：`wtpy-master`  
目标版本：现有外挂数据盘零迁移、现有配置零变更、日常只做增量同步  
实施分工：DeepSeek 负责代码实施，Codex 负责独立复核与验收

---

## 1. 决策结论

系统正式采用 **Tushare-only 运行策略**：

1. 所有新增股票、指数、基金、复权因子和退市股票数据均从 Tushare 获取。
2. 正式 L2 执行面继续使用 `internal/composite_none`。
3. 正式 L1 信号面继续使用 `internal/composite_tushare_factor_qfq`。
4. `internal/composite_none` 的 base 从 `local_vendor/none` 切换为 `tushare/none`。
5. 退市股票 supplement 也必须来自 Tushare，并且只包含 base 中缺失的股票。
6. 通达信、`local_vendor` 的 provider 代码和历史 manifest 暂时保留，但退出默认同步、查询、回测和 UI 链路。
7. 不迁移外挂数据盘，不修改现有必填环境变量，不引入新的基础设施和第三方依赖。
8. 朋友服务器升级后只需拉取代码并重启，下一次 Tushare 增量任务自动完成产品面协调。

这里的 “Tushare-only” 指外部数据供应商统一为 Tushare。内部复合数据集仍使用
`source=internal`，以表达其产品数据集和数据血缘语义。

---

## 2. 强制约束

### 2.1 必须保持兼容

- 保留现有 `MARKET_DATA_ROOT`。
- 保留现有 Tushare Token 读取方式。
- 保留外挂盘现有目录：
  - `manifests`
  - `blobs`
  - `universes`
  - `sync_logs`
- 保留已有 dataset ID 和旧 manifest 的可读性。
- 保留旧回测通过明确 `dataset_id` 复现历史结果的能力。
- 不要求朋友服务器修改 `.env`、定时任务、磁盘挂载、服务管理配置。
- 不在 FastAPI 启动阶段执行数小时的网络全量下载。
- 不删除或重写用户已有 blob。

### 2.2 不允许的简化

- 不允许把旧的 local_vendor composite 仅修改标签后冒充 Tushare composite。
- 不允许把 `tdx_front` 静默映射为 Tushare QFQ。
- 不允许直接使用仅包含当前上市股票的 `tushare/none` 作为正式 survivorship-safe L2。
- 不允许同步任务只因 API 请求成功就标记整体成功。
- 不允许新数据日期回退、行数严重下降或出现短窗口孤儿面时 supersede 旧 ready 面。
- 不允许为了升级而要求重新下载全市场完整历史。

---

## 3. 原始问题闭环

### 3.1 用户现象

`600000`、`000001`、`600519` 的快速查询长期停留在 `20260717`，但每晚同步任务显示成功。

改造后的闭环：

1. Quick/raw 不再参与 local_vendor、TDX 和 Tushare 的模糊竞争。
2. Quick/raw 默认读取当前正式 L2；产品面尚未完成时可临时读取完整的
   `tushare/none`，并返回 `bootstrap_fallback=true`。
3. 正式 L2 由最新 Tushare raw + Tushare 退市补集组成。
4. 数据过期时 Dashboard 和同步状态必须明确告警。

### 3.2 三层根因

| 根因 | 已有修复 | 本次最终治理 |
|---|---|---|
| local_vendor 停供 | 无法从代码修复上游 | 从正式运行链路移除 |
| Tushare 6000 条截断 | 三年分页、排序、去重 | 保留修复，增加完整性门槛 |
| 查询优先旧 local_vendor | 最新查询 cutoff 优先 | 进一步改为产品角色优先、同角色内按实际日期排序 |

### 3.3 已完成代码修复的保护要求

以下修复不得回退：

1. `tushare.py` 的四类历史接口按三年窗口分页、升序排序、去重。
2. `sync_market_data.py` 的 `_infer_incremental_resume()`。
3. 增量窗口与完整历史父面合并。
4. UI derive 自动解析父数据集。
5. `BaguaPlaneSession._score` 在 `asof=None` 时优先新鲜度。

实施前必须阅读这些文件的当前未提交 diff，禁止用旧版本覆盖。

---

## 4. 目标数据架构

```text
Tushare daily / index_daily / fund_daily
                    |
                    v
             tushare/none
                    |
                    +-------------------------------+
                    |                               |
                    |                 Tushare delisted-only
                    |                 missing complement
                    |                               |
                    +---------------+---------------+
                                    |
                                    v
                      internal/composite_none
                              正式 L2
                                    |
                         tushare/adj_factor
                                    |
                                    v
             internal/composite_tushare_factor_qfq
                              正式 L1
```

### 4.1 正式数据角色

| 产品用途 | 数据角色 |
|---|---|
| Quick/raw | 当前正式 L2 |
| 卦象 raw | 当前正式 L2 |
| 卦象前复权 | 当前正式 L1 |
| 回测执行价格 | 当前正式 L2 |
| 回测信号价格 | 当前正式 L1 |

### 4.2 原子产品对

不新增数据库或复杂发布系统。使用现有 lineage 实现原子产品对：

1. 选择最新 ready 且 `data_policy=tushare_only_v1` 的 L1。
2. 从 L1 manifest 的 raw parent 解析 L2。
3. 校验 L2 ready、无复权、数据策略正确。
4. 校验 L2 的 base/supplement 均来自 Tushare。
5. 只有这组 L1/L2 同时通过才成为默认正式产品面。

这样可以避免：

- L2 已更新而 L1 派生失败时默认回测使用不一致父集。
- Dashboard 展示 raw 最新日期，但正式回测仍停留在旧日期。
- Quick、卦象、回测分别选择不同数据版本。

---

## 5. 零配置增量升级

### 5.1 朋友服务器升级动作

服务器侧只执行：

```powershell
git pull
# 使用现有方式重启服务
```

不需要执行：

- 数据盘迁移。
- `.env` 修改。
- Token 重新配置。
- 手工指定父 dataset ID。
- 全历史重新下载。
- 删除旧 manifest。

### 5.2 第一次升级后的自动行为

1. 服务启动后只做快速本地检查，不立即全量联网同步。
2. 如果外挂盘已有完整 Tushare raw、factor 和退市数据：
   - 自动在后台构建 Tushare composite。
   - `composite_none` 只引用现有 blob。
   - QFQ 使用现有 raw/factor 派生。
3. 如果父数据缺少最新日期：
   - 等待现有 Tushare 定时任务。
   - 只下载最近增量窗口。
   - 合并已有完整历史父面。
   - 自动协调 L1/L2。
4. 新产品对未全部 ready 前不进行正式切换。
5. 协调失败时保留旧 ready 数据集并显示明确错误。

### 5.3 初次生成时间

基于现有本地结果：

- `composite_none`：主要生成 manifest 和引用，通常数秒到几十秒。
- 约 5554 只股票的 composite QFQ：本地实测约 44 秒。
- 普通 SSD 服务器保守预计 1～3 分钟。
- 机械硬盘预计 3～10 分钟。
- 慢速网络盘可能更长，但不应触发全历史网络下载。

### 5.4 可复用与不可复用

| 对象 | 策略 |
|---|---|
| `tushare/none` | 直接复用 |
| `tushare/adj_factor` | 直接复用 |
| Tushare delisted 数据 | 直接复用 |
| 现有 Tushare blobs | 直接复用 |
| local_vendor-based `composite_none` | 不作为新正式面复用 |
| 以旧 composite 为 raw parent 的 L1 | 不作为新正式面复用 |
| 直接 `tushare/qfq` | 可保留诊断，不作为正式 L1 |

---

## 6. 同步任务编排

### 6.1 新的统一任务

建议增加共享函数：

```python
def reconcile_tushare_product_datasets(
    store: DatasetStore,
    *,
    requested_cutoff: int | None = None,
    dry_run: bool = False,
) -> ProductReconcileResult:
    ...
```

不要把所有逻辑堆叠在 FastAPI route。协调逻辑应位于
`wtpy/apps/astock/data/` 下的共享模块，由 CLI/API 调用。

### 6.2 标准执行顺序

```text
1. Tushare raw 增量同步
2. Tushare adj_factor 增量同步
3. Tushare delisted 元数据和缺失股票增量同步
4. 构建 delisted missing complement
5. 构建 internal/composite_none
6. 派生 internal/composite_tushare_factor_qfq
7. 产品对一致性校验
8. 数据健康校验
9. 发布本次同步最终状态
```

父数据不齐时协调器返回结构化状态，不得生成半成品正式面：

```json
{
  "status": "waiting_for_parent",
  "missing": ["tushare/adj_factor"],
  "published": false
}
```

### 6.3 幂等要求

父 manifest ID、manifest hash、cutoff 和策略版本均未变化时：

```json
{
  "status": "up_to_date",
  "published": false
}
```

不得重复计算和重复发布相同产品。

### 6.4 旧任务兼容

| 旧入口 | 新行为 |
|---|---|
| `task=tushare` | Tushare 增量 + 自动协调 |
| `task=factor` | factor 增量 + 自动协调 |
| `task=derive` | 自动解析父集并协调 |
| `source=all` | 只运行 Tushare 相关任务 |
| `task=tdx` | 返回成功的 `skipped/disabled_by_policy`，不访问 TDX |

如果系统已有独立 Tushare 定时任务，不需要修改定时配置。

---

## 7. Survivorship-safe composite

### 7.1 Base

选择最新满足以下条件的 `tushare/none`：

- `status=ready`
- 完整历史数据集，不是短窗口孤儿面
- symbol_count 和历史行数通过质量检查
- cutoff 不早于当前正式产品面
- 所有引用 blob 存在

### 7.2 Supplement

Supplement 必须是：

```text
Tushare delisted symbols - base symbols
```

按整只股票选择，禁止跨数据集拼接单只股票的不同日期区间。

如果 base 已包含某只退市股票：

- 不把它加入 supplement。
- 记录为 `excluded_overlap`。
- 不放宽 `build_composite_none()` 的严格重叠校验。

### 7.3 Manifest metadata

新 composite 至少记录：

```json
{
  "data_policy": "tushare_only_v1",
  "survivorship_policy": "listed_plus_delisted",
  "base_source": "tushare",
  "supplement_source": "tushare",
  "supplement_rule": "missing_symbols_only",
  "parent_dataset_ids": [],
  "parent_manifest_sha256": {},
  "quality_status": "passed"
}
```

`composite_dataset.py` 中关于 local_vendor 的硬编码文档、warning 和 provenance
说明必须改成基于真实父 manifest 生成。

---

## 8. 查询与回测选面

### 8.1 候选集先按产品角色限制

`BaguaPlaneSession` 不再让 local_vendor、TDX、Tushare 在同一个分数中自由竞争。

建议候选顺序：

```text
raw:
  1. 当前正式 L2
  2. 最新完整 tushare/none，仅用于 bootstrap/quick fallback

tushare_qfq:
  1. 当前正式 L1
  2. 不建议正式 fallback；缺失时返回产品面未就绪
```

正式回测不得使用 survivorship-unsafe fallback。

### 8.2 `_score` 设计

先执行硬性 eligibility：

- manifest ready。
- symbol blob 存在。
- 产品策略匹配。
- symbol 日期覆盖请求。
- 质量状态通过。
- 非短窗口孤儿面。

`asof=None` 时在同产品角色内按以下顺序排序：

```python
(
    symbol.max_date,
    manifest.data_cutoff_date,
    history_completeness,
    symbol.row_count,
    manifest.created_at,
)
```

历史 `asof`：

1. 必须满足 `min_date <= asof <= max_date`。
2. 对时点锚定 QFQ，优先 cutoff 不早于 asof 且距离 asof 最近的数据集。
3. 保留确定性 tie-breaker。
4. 不再使用 `-source_priority` 压过新鲜度。

### 8.3 API 默认值

- `/api/v1/quick/{code}`：raw 指向正式 L2。
- `/api/v1/bagua/query`：默认从 `tdx_front` 改为 `tushare_qfq`。
- 回测和实验的 execution 默认改为正式 L2。
- `tdx_front` 显式请求返回清晰的 disabled/deprecated 错误，不静默替换语义。

### 8.4 元数据

API 返回至少包含：

```json
{
  "dataset_id": "...",
  "dataset_source": "internal",
  "dataset_adjustment": "none",
  "data_policy": "tushare_only_v1",
  "data_max_date": 20260804,
  "bootstrap_fallback": false
}
```

---

## 9. 发布质量门槛

### 9.1 禁止日期回退

对于同一产品角色，新候选实际数据日期早于当前 ready 面时：

- 不 supersede。
- 标记 `failed` 或 `partial`。
- 输出 `data_date_regression`。

判断必须使用 symbol 记录的真实日期分布，不能只看 manifest cutoff。

### 9.2 防止短窗口孤儿面

不再只依赖 `avg rows < 500` 的启发式过滤。发布前至少检查：

- 是否有完整历史父集。
- 新数据是否正确合并父历史。
- symbol 行数相对父集是否异常下降。
- 全市场 row_count 中位数是否严重下降。
- history_start 是否出现大范围突然后移。
- 本次 sync_mode 是否为增量窗口但缺少 parent lineage。

不合格数据集不得进入正式候选；旧的孤儿面可自动标记 superseded，并记录原因。

### 9.3 结构和行情校验

- 交易日期升序。
- 日期去重。
- OHLC 基本关系正确。
- volume/amount 非法值检查。
- blob 存在且可读。
- symbol_count/total_rows 不发生异常回退。
- sentinel 股票和指数达到预期交易日。

### 9.4 发布事务语义

同步最终成功必须满足：

```text
下载成功
AND raw manifest 通过
AND factor manifest 通过
AND composite L2 通过
AND L1 派生通过
AND 产品对一致
AND freshness 通过
```

否则任务不得显示“全部成功”。

---

## 10. 数据健康监控

### 10.1 健康接口

增加或扩展：

```text
GET /api/v1/system/data-health
```

返回：

- 预期最新完成交易日。
- Tushare raw 实际日期。
- factor 实际日期。
- delisted supplement 状态。
- 正式 L2 日期和 dataset ID。
- 正式 L1 日期和 dataset ID。
- L1/L2 父链一致性。
- 交易日滞后数量。
- stale/warning/healthy 状态。
- 最近同步失败原因。
- 历史回填进度。

### 10.2 交易日感知

不得按自然日简单计算过期。

- 使用交易日历确定预期最新交易日。
- 当日数据尚未达到发布 SLA 时，预期日期仍是上一个交易日。
- 市场整体新鲜度使用指数/sentinel 和日期分布判断，避免停牌股票造成误报。

### 10.3 Dashboard

Dashboard 不再显示 `latest_local_vendor`，而是显示：

| 数据角色 | dataset ID | 实际日期 | 状态 |
|---|---|---:|---|
| Tushare raw | ... | ... | ... |
| Tushare factor | ... | ... | ... |
| 正式 L2 | ... | ... | ... |
| 正式 L1 | ... | ... | ... |

P0 告警不依赖新增配置：

- UI 红色状态。
- API 结构化错误。
- sync log 明确错误。
- 进程/任务返回失败状态。

Webhook、邮件等外部通知列为后续可选能力，不作为本次升级前置条件。

---

## 11. 2001 年以前历史缺口

普通最近日期增量不会自然补齐 2001 年以前的数据。

本次不允许首次启动直接触发 2～6 小时全量下载，采用两条独立通道：

### 11.1 日常最新增量

- 优先更新最新交易日。
- 完成后立即发布当前产品面。
- 不被历史回填阻塞。

### 11.2 向前分批回填

- 使用同一 Tushare 分页实现。
- 每次固定处理少量股票或固定 API 预算。
- 保存 checkpoint。
- 可跨多晚完成。
- 回填完成后与现有父面合并。
- 不要求用户增加配置。
- 提供独立维护命令用于需要立即完成的部署，但默认不执行长时间全量任务。

健康接口必须区分：

- `current_freshness`
- `historical_completeness`

不能因为最新数据正常就隐藏历史仍不完整的事实。

---

## 12. Legacy 数据源治理

### 12.1 本次处理

- UI 隐藏 TDX、tdx_local、local_vendor 同步入口。
- `source=all` 不再调用 legacy provider。
- 默认查询候选移除 legacy 数据源。
- 默认回测候选移除 legacy 数据源。
- 旧 provider、manifest 和 blob 不删除。
- 显式旧 dataset ID 仍可用于历史复现。

### 12.2 local_vendor 发布回退问题

由于 local_vendor 退出正式链，不再投入供应商专用重构。但通用发布门槛必须覆盖：

- 日期不得回退。
- 覆盖率不得严重下降。
- 导入失败不得 supersede。
- 新文件不完整时旧 ready 面继续保留。

这样未来新增任何供应商也不会重现相同问题。

### 12.3 后续删除

物理删除 TDX/local_vendor provider 属于 major version 清理，不纳入本次最小改造。

---

## 13. 代码落点

### 13.1 核心文件

| 文件 | 改动 |
|---|---|
| `scripts/sync_market_data.py` | Tushare 增量后自动协调；`all` 单源化；旧任务兼容 |
| `wtpy/apps/astock/data/providers/tushare.py` | 保留分页和排序修复；补充完整性 metadata |
| `wtpy/apps/astock/data/composite_dataset.py` | 父源通用化；Tushare complement；策略 metadata |
| `wtpy/apps/astock/data/dataset_binding.py` | 默认角色改为 Tushare 产品面 |
| `wtpy/apps/astock/service/baseline.py` | 校验 Tushare-only lineage 和 L1/L2 一致性 |
| `wtpy/apps/astock/service/bagua_query.py` | 统一候选、score、legacy fallback 策略 |

建议新增一个小型共享模块，避免脚本、API、baseline 重复解析：

```text
wtpy/apps/astock/data/tushare_product.py
```

建议职责：

- 解析完整 Tushare 父集。
- 创建 delisted missing complement。
- 协调 L2/L1。
- 解析当前正式产品对。
- 校验 lineage。
- 返回结构化协调结果。

### 13.2 API 和 UI

| 文件 | 改动 |
|---|---|
| `wtpy/apps/astock/api_routes/system.py` | 统一任务、health API、启动协调 |
| `wtpy/apps/astock/api_routes/bagua.py` | 默认 `tushare_qfq` |
| `wtpy/apps/astock/api_routes/backtests.py` | execution 默认正式 L2 |
| `wtpy/apps/astock/api_routes/experiments.py` | execution 默认正式 L2 |
| `wtpy/apps/astock/api.py` | 启动信息展示正式产品面 |
| `wtpy/apps/astock/research/signal_cache.py` | 移除 local_vendor fallback metadata |
| `wtpy/apps/astock/service/backtest_artifacts.py` | 保存真实产品数据血缘 |
| `wtpy/apps/astock/web/static/index_v3.html` | Tushare-only 同步和健康状态 |
| `wtpy/apps/astock/web/static/quick.html` | 显示真实 dataset 和 freshness |

### 13.3 测试

重点修改或新增：

- `test_composite_dataset.py`
- `test_backtest_source_selection.py`
- `test_bagua_query.py`
- `test_bagua_index_etf.py`
- `test_survivorship_chain_wiring.py`
- `test_web_api.py`
- Tushare pipeline/reconcile 新测试
- 数据健康和日期回退新测试

---

## 14. 实施顺序

### P0-1：产品数据链

1. 增加 Tushare 产品协调模块。
2. 生成无重叠退市补集。
3. Tushare base 构建 `composite_none`。
4. 自动派生 composite QFQ。
5. 实现幂等和父链校验。

验收：能够仅使用现有外挂盘 Tushare 数据生成一致的 L1/L2。

### P0-2：默认选面切换

1. baseline 强制产品对一致。
2. Quick/raw 使用正式 L2。
3. Bagua 默认使用 Tushare QFQ。
4. 回测/实验默认使用正式 L2。
5. legacy 数据只允许显式 dataset ID。

验收：即使 local_vendor/TDX manifest 更新日期更大，也不会被默认选中。

### P0-3：质量和孤儿面治理

1. 日期回退阻断。
2. 短窗口数据阻断。
3. 父历史合并校验。
4. 旧孤儿面自动 supersede。
5. 最终任务成功语义收紧。

验收：构造 16 行面或日期回退面时旧正式产品不切换。

### P0-4：健康监控和 UI

1. data-health API。
2. Dashboard 正式 L1/L2。
3. 交易日滞后告警。
4. 同步失败原因。
5. 移除 TDX/local_vendor 默认入口。

验收：模拟 Tushare 停更后 UI/API 在 SLA 后明确变红。

### P1：历史渐进回填

1. 增加向前回填 checkpoint。
2. 每次固定预算。
3. 不阻塞日常最新同步。
4. Dashboard 显示历史完整率。

---

## 15. 验证计划

### 15.1 自动测试

执行：

```powershell
python -m pytest tests/apps/astock -q
```

已知历史基线为 991 passed。实施后必须报告新的实际结果，不能只引用旧结果。

新增测试必须覆盖：

1. `source=all` 不初始化任何 TDX/local_vendor provider。
2. Tushare 增量成功后自动协调。
3. 父集未变化时 reconcile 幂等。
4. `composite_none` base 为 Tushare。
5. supplement 与 base 无重叠。
6. 退市股票存在于正式 L2。
7. L1 raw parent 与正式 L2 一致。
8. 新数据日期回退时拒绝切换。
9. 16 行孤儿面不能被选中。
10. Quick 不选择 local_vendor/TDX。
11. Bagua 默认不是 `tdx_front`。
12. 显式 `tdx_front` 返回清晰错误。
13. 旧 dataset ID 仍可加载。
14. 无新增环境变量时正常启动。
15. 产品面尚未就绪时正式回测不使用 survivorship-unsafe fallback。

### 15.2 本地冒烟

启动：

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

截至 2026-08-05，当前本地验收目标为：

- 三只股票最新交易日为 `20260804`。
- raw metadata 指向 Tushare-only 正式 L2，或在短暂 bootstrap 期间明确标记 fallback。
- QFQ metadata 指向正式 L1。
- 正式 L1/L2 lineage 一致。
- Dashboard 不再以 universe 最大日期冒充正式回测面日期。

### 15.3 故障注入

必须模拟：

1. Tushare API 请求失败。
2. raw 比旧 ready 面日期更早。
3. 只生成最近 16 行。
4. factor 未更新。
5. QFQ 派生失败。
6. delisted supplement 与 base 重叠。
7. blob 缺失。

期望：

- 旧正式产品继续可读。
- 新产品不切换。
- 同步状态不是 success。
- health API 和 Dashboard 显示明确原因。

### 15.4 升级验证

必须至少测试两种数据盘：

1. 已有 local_vendor/TDX/Tushare 混合历史数据的升级盘。
2. 仅按 GitHub 安装文档建立、主要使用 Tushare 的数据盘。

验证两者都不需要修改配置和目录。

---

## 16. 发布与回滚

### 16.1 发布

1. 在当前分支完成最小代码修改。
2. 运行定向测试。
3. 运行 `tests/apps/astock` 全量测试。
4. 本地真实外挂盘执行增量和冒烟。
5. Codex 独立审查 diff、测试和运行证据。
6. 朋友服务器 `git pull` 并重启。
7. 等待或手动触发原 Tushare 增量任务。
8. 检查 data-health、Quick、L1/L2 lineage。

### 16.2 回滚

- 不删除旧 manifest/blob，因此代码回滚不会丢失数据。
- 新 internal manifest 是增量新增，不覆盖历史文件内容。
- 回滚代码后旧明确 dataset ID 仍可使用。
- 禁止使用 `git reset --hard` 或删除外挂盘数据作为回滚手段。

---

## 17. DeepSeek 与 Codex 的交付边界

### DeepSeek 负责

- 阅读当前代码和未提交 diff。
- 按本文档实施最小代码修改。
- 增加或修改测试。
- 运行定向测试和全量测试。
- 提供变更文件、关键设计、命令输出、未验证项。
- 不修改实际生产外挂盘数据，除非用户明确授权。

### Codex 负责复核

- 核对是否真正 Tushare-only，而非仅改 UI 标签。
- 核对 survivorship-safe 和退市补集。
- 核对 L1/L2 时点锚定和父链一致性。
- 核对已有分页、resume、score 修复未回退。
- 核对无新增必填配置、无数据盘迁移。
- 核对日期回退、孤儿面和 freshness 告警。
- 独立运行测试和 API 冒烟。
- 给出接受或不接受结论及阻塞项。

---

## 18. 最终验收标准

只有同时满足以下条件才算完成：

1. 默认运行链不访问 TDX/local_vendor。
2. 日常任务只做 Tushare 增量，不要求全量重建。
3. 第一次升级自动使用现有 Tushare 数据生成产品面。
4. `composite_none` survivorship-safe。
5. L1/L2 来自同一 Tushare-only lineage。
6. Quick、Bagua、回测默认选面统一。
7. 数据日期回退和短窗口孤儿面不能切换为正式面。
8. 数据停更在 SLA 后自动显示异常。
9. Dashboard 显示真实正式面日期。
10. 无新增必填环境变量和目录迁移。
11. 现有测试和新增测试全部通过。
12. 朋友服务器只需更新代码、重启并继续原增量任务。

