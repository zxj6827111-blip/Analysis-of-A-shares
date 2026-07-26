# A股回测系统多行情源与多前复权口径改造 — 独立验收报告

**验收角色**: 独立代码审查员 / 验收测试工程师  
**验收日期**: 2026-07-25  
**项目目录**: `E:\Software Development\wtpy-master`  
**原则**: 仅审查与测试；不修改正式代码；不自动修复；不 commit/push；不删除缓存/数据集/历史实验；不采信开发 AI 总结为事实。

---

## 1. 最终结论

### **FAIL — 未完成**

本轮改动在 **Provider 抽象、DatasetStore、同步脚本与大量单元测试** 层面有实质骨架，但 **回测主路径、实验中心后端、API、信号缓存生产调用、runs 落库** 均未把 `signal_data_source` / `dataset_id` / Repository 真正接入。页面下拉框与 DTO 字段存在，**切换数据源不会改变实际 L1 行情读取**。

触发 FAIL 标准（验收要求第二十一节）中的多项：

| 标准 | 证据摘要 |
|------|----------|
| 11. 页面只是增加下拉框，底层没有真实切换 | `index_v3.html` 提交 `signal_data_source`，但 `BacktestBody` / `create_experiment` / `experiments.BacktestRequest(...)` 均不接收；`backtest.py` 仍只走 affine/asof + 本地 raw |
| 12. 双源实验两个 variant 实际读取同一数据 | 双源“测试”仅在测试文件内手写 dict 拼装，**未**改 `experiments.py` 网格扩展逻辑 |
| 13. 真实接口功能没有任何验收级 live 通过证据 | `@pytest.mark.live_tdxquant` 未注册；无 `live_tushare`；全量 `pytest` 未跑通 live；开发报告自承 live 未执行 |
| 4. dataset_id 未锁定（运行链路） | `BacktestRequest.dataset_id` 仅字段存在；创建/运行路径不 resolve、不锁定、不读 Repository |
| （相关）缓存隔离生产失效 | `signal_cache_key` 虽支持新字段，但 `backtest.py::_make_signal_cache_key` **不传** `data_source`/`dataset_id` |
| （相关）DB 新列未写入 | `SCHEMA` 有 6 列且有 v1→v2 迁移，但 `upsert_run_from_index_row` 的 `INSERT` **不含**这些列 |

**不建议合并**到主线用于生产多源回测。

---

## 2. Git 状态

| 项 | 值 |
|----|-----|
| 当前分支 | `feat/multi-source-market-data` |
| 当前 commit (HEAD) | `cad0742746aa6f675b6fc4b17798ee43b482c2df` |
| 基准 commit | `cad0742` = `fix/standard-qfq-raw-execution`（与 `origin/fix/standard-qfq-raw-execution` 同点） |
| `cad0742` 是否为 HEAD 祖先 | 是（`HEAD == cad0742`，工作区为未提交改动） |
| 是否基于 361 测试基线分支 | **是**（未从落后 `main`/`55510c5` 开叉；`main` 更旧） |
| 工作区 | **不干净**（大量未跟踪文件 + 7 个已修改文件） |
| 是否误改原始行情数据文件 | **未发现**对 `storage/astock/csv` 等的 git 修改 |
| Token 进入 git 跟踪内容 | **未发现**明文 token（见安全节） |
| 旧仿射模型语义 | **未改** `affine_adjust` 核心；`InternalAsOfProvider` 为新增包装 |
| 不应提交的大型/探测文件 | 根目录 `SZSE.399*.csv`；`tmp/tdxquant_probe/*`；`tmp/` 下 JSON |

### 2.1 改动范围统计

| 类别 | 数量 / 说明 |
|------|-------------|
| 已跟踪修改 (M) | **7** 文件 |
| 未跟踪新代码 | providers×6、`dataset_store.py`、`repository.py`、`scripts/sync_market_data.py` |
| 未跟踪测试 | **19** 个 `tests/apps/astock/test_*.py`（清单见下） |
| 未跟踪文档 | 审计/实施报告 3 份 |
| 删除文件 | **0**（git name-status 无 D） |
| 无关可疑 | 根目录指数 CSV；`tmp/tdxquant_probe` 探测脚本；未忽略的 tmp 导致 **默认 pytest 收集失败** |

**修改文件 (M)**:

- `wtpy/apps/astock/data/universe.py`
- `wtpy/apps/astock/research/signal_cache.py`
- `wtpy/apps/astock/service/backtest_context.py`
- `wtpy/apps/astock/service/backtest_request.py`
- `wtpy/apps/astock/service/db.py`
- `wtpy/apps/astock/study.py`
- `wtpy/apps/astock/web/static/index_v3.html`

**新增（未跟踪）核心代码**:

- `wtpy/apps/astock/data/providers/{__init__,base,tdx_local,tdxquant,tushare,internal_asof}.py`
- `wtpy/apps/astock/data/dataset_store.py`
- `wtpy/apps/astock/data/repository.py`
- `scripts/sync_market_data.py`

---

## 3. 交付材料核查

| 材料 | 状态 |
|------|------|
| `docs/market_data_multi_source_audit.md` | 存在（审计基线，编码在部分终端乱码，内容可读于文件） |
| `docs/market_data_multi_source_audit_addendum.md` | 存在 |
| `tmp/market_data_multi_source_audit.json` | 存在（记录改造前 `tdxquant_provider_exists: false`） |
| `tmp/market_data_multi_source_benchmark.json` | 存在 |
| `docs/multi_source_market_data_implementation_report.md` | 存在（**开发自述**，不得单独作为通过依据） |
| `tmp/multi_source_market_data_test_results.json` | 存在（声称 510 passed；**与本轮复测不一致处见第 16 节**） |
| `tmp/multi_source_market_data_changed_files.txt` | 存在 |

---

## 4. Gate 1～5 状态

> 状态仅允许：PASS / PARTIAL / FAIL / NOT IMPLEMENTED / NOT VERIFIED

### Gate 1 — Provider / Repository / 域模型 / 旧行为兼容

| 子项 | 状态 | 证据 |
|------|------|------|
| Provider 抽象层 | **PASS**（库层） | `providers/base.py`: `MarketDataProvider` Protocol：`health_check` / `capabilities` / `fetch_bars` / `fetch_universe` / `provider_version`；枚举 `DataSource`/`AdjustmentMode`/`BarPeriod`/`WeeklyBarMode`；错误类型族 |
| 四个 Provider | **PARTIAL** | 文件均存在：`tdx_local`/`tdxquant`/`tushare`/`internal_asof`；单元测试覆盖协议与规范化；**业务层未统一经 Provider 取数** |
| Repository | **PARTIAL** | `repository.py` 实现 list/resolve/load；**无任何 service/backtest 导入**（全库引用仅 repository 自身、base 注释、tests、sync 脚本） |
| 数据集模型 | **PARTIAL** | `dataset_store.py`：manifest、blob sha、status、publish；见 P1 关于 partial 可读 |
| 旧行为兼容 | **PARTIAL** | 默认回测仍走原 L1 asof + L2 raw（因未接线，行为碰巧未变）；DB 迁移标记 legacy；**新列 upsert 未写** |

**Gate 1 总评: PARTIAL**

### Gate 2 — 同步 / 双 dataset / 301107 / 原子发布

| 子项 | 状态 | 证据 |
|------|------|------|
| TdxQuant 同步 | **PARTIAL** | `scripts/sync_market_data.py` `sync_tdxquant_full`；需客户端；**本轮未 live 验收** |
| Tushare 同步 | **PARTIAL** | `sync_tushare_full`；无 `adj_factor` 专用拉取；incremental 实质仍 `_sync_dataset` 全量式拉取 |
| 两套独立 dataset | **PARTIAL** | `make_dataset_id(source, adjustment, period, …)` 可区分；**无真实双源 ready 数据集本轮证据** |
| 301107 回归 | **PARTIAL** | 测试用 **mock 宽表** 断言 20.15/20.25/19.15/19.65；**未**断言真实日期 2026-05-11～15 的 live 序列；live 标记未注册 |
| 原子发布 / partial | **PARTIAL** | `DatasetStore.publish` 可置 ready/partial/failed；`resolve_latest_ready` 仅 ready；但 `load_bars` **允许 partial**（见 P0/P1） |

**Gate 2 总评: PARTIAL**（live 未验证 → 不可 PASS）

### Gate 3 — 回测 source / 锁定 / 缓存 / 旧任务

| 子项 | 状态 | 证据 |
|------|------|------|
| 回测 source 选择 | **FAIL** | `backtest.py` 无 `signal_data_source`；API `BacktestBody` 无字段；实验 runner 构造 `BacktestRequest` 时忽略新字段 |
| dataset_id 锁定 | **FAIL** | 仅 DTO/测试断言字段可序列化；无“创建时 resolve → 写入 → 运行只读该 id”路径 |
| 信号缓存隔离 | **PARTIAL→生产 FAIL** | `signal_cache_key` 参数完备且单测通过；**生产调用不传隔离字段** |
| 执行缓存隔离 | **PARTIAL→生产 FAIL** | `backtest_context.apply_execution_cache` 已写入 payload 字段；**上游 req 几乎总为空默认** |
| 旧任务兼容 | **PARTIAL** | 迁移脚本将 NULL → `legacy_tdx_local_asof`；单测用临时 DB；**upsert 仍不持久化新字段**，新任务也写不进列 |

**Gate 3 总评: FAIL**

### Gate 4 — 实验中心 UI / 双源 / 结果追溯

| 子项 | 状态 | 证据 |
|------|------|------|
| 数据源选择 UI | **PARTIAL** | `index_v3.html` 有下拉：tdxquant/tushare/internal |
| dataset 信息展示 | **NOT IMPLEMENTED** | 前端无 cutoff/anchor/coverage/status 展示 |
| 周线模式选择 | **PARTIAL** | UI + `study.build_period_bars` 参数；**引擎未传 `weekly_bar_mode` 贯通** |
| 双源对照实验 | **FAIL** | UI checkbox `dual_source_compare`；`experiments.py` **零匹配** dual_source；测试自建 variants |
| 结果页来源追溯 | **NOT IMPLEMENTED** | 未验证结果页绑定 `dataset_id`；DB upsert 也不写 |

**Gate 4 总评: FAIL**

### Gate 5 — 北交所 / 退市 / 100 股双源 / 差异报告

| 子项 | 状态 | 证据 |
|------|------|------|
| 北交所可选 | **PARTIAL** | `is_bse_code` / `from_tdx_dirs(include_bj)` / Tushare universe filter；单测存在 |
| 退市可选 | **PARTIAL** | `SymbolInfo.status` + `from_tushare_basic(include_delisted)`；单测存在 |
| 100 只双源验收 | **NOT VERIFIED** | 无本轮运行证据；开发报告亦写未执行 |
| 差异报告 | **NOT IMPLEMENTED** | 无对比报告生成链路 |

**Gate 5 总评: PARTIAL / NOT VERIFIED**

---

## 5. Provider 审查

### 5.1 统一接口

`wtpy/apps/astock/data/providers/base.py` 定义完整 Protocol 与 `MarketBar`/`MarketDataRequest`。**通过（抽象层）**。

### 5.2 业务层是否仍直接 tqcenter / Tushare

- 回测服务 **不** 调用 tqcenter/Tushare（也 **不** 调用新 Provider）。
- 外部调用封装在 `TdxQuantProvider` / `TushareProvider` / `scripts/sync_market_data.py`。
- 正式回测仍通过 **原 DataStore / affine / TdxDayReader 路径** 读本地数据（改造前路径）。

判定：**回测服务未直接调用外部 API**（好），但 **也未接入 Repository**（坏）→ 多源目标未达成。

### 5.3 静默 fallback

- Provider 间：`test_provider_no_silent_fallback.py` 覆盖“不跨源补数”。
- `TdxQuantProvider._fetch_singles`：单股失败 **logger.warning 后跳过**，不抛出 → 调用方可能得到 **残缺 bars 且无硬错误**（P1）。
- `sync_market_data` 批量失败时对整批记 error，**未**在 sync 层拆单重试（拆单逻辑在 provider 内部分路径）。

### 5.4 TdxQuantProvider

| 要求 | 结论 |
|------|------|
| 一次初始化 | 是（`_ensure_initialized`） |
| 默认 batch 10～20 | 构造/`--batch-size` 默认 10 |
| 批失败拆单 | provider 内有；sync 批路径整批失败记 error |
| 最大重试 | `MAX_RETRIES` 受控 |
| 客户端状态区分 | 部分字符串匹配 login/connect |
| none/front + 1d/1w | 支持 |
| 不混写 Tushare | 源字段隔离；依赖调用方 |
| provider_version / cutoff | MarketBar 可带；sync 写入 manifest |
| 失败当成功空数据 | 宽表缺列 `continue`；单股失败可能静默缺数据 |

### 5.5 TushareProvider

| 要求 | 结论 |
|------|------|
| Token 不打印 | 代码未见 print token；测试用 `"fake"` |
| Token 不进 config.json | 未见写入 |
| 兼容 get_token | `ts.get_token()` / 入参 |
| daily / pro_bar qfq | 有 |
| stock_basic L/D | universe 有 |
| **adj_factor** | **未实现** `pro.adj_factor` 调用 |
| QFQ anchor_date | 可从 request 传入；sync 依赖 `--anchor-date` |
| Permission 不重试 | 识别后 raise |
| RateLimit 退避 | 有 |
| 因子变化重建全历史 | **未真实实现**（incremental 未按 adj 变化筛选重建） |

### 5.6 InternalAsOfProvider

包装 `build_affine_series` + asof 仿射；保留因果 asof 语义方向正确。**未接入回测主路径**。

---

## 6. Repository 与 dataset 审查

**实现亮点（代码层）**:

- `make_dataset_id(source, adjustment, period, cutoff_or_anchor, manifest_sha)`
- blob 内容寻址 NPZ
- `publish` 完整性检查失败 → failed
- `resolve_latest_ready` 过滤 `status="ready"`

**严重缺口**:

1. **生产零引用** `MarketDataRepository`（除 sync/tests）。
2. `load_bars` 允许 `status in ("ready", "partial")` → partial 可被显式 id 读取（若将来接线，违反“partial 不可回测”的严格解读）。
3. `dataset_id` 生成用 `source.replace("_","")` → `tdxlocal` 等，与文档示例略有差异（P2）。
4. 任务创建锁定：**无**。

路径追踪（要求的链路）:

```
实验创建 (UI dual_source / signal_data_source)
  → experiments.create_experiment_from_grid  【未处理新字段】
  → variant params
  → BacktestRequest(...)  【未传入 signal_data_source/dataset_id】
  → BacktestService.run / backtest.py  【仍 affine+local】
  → MarketDataRepository  【从未调用】
```

---

## 7. 回测运行时外部接口隔离

| 检查 | 结果 |
|------|------|
| 静态：backtest 调 Provider.fetch_bars | **否** |
| 自动化：完整回测 + mock Provider 抛错仍成功 | **仅** `test_backtest_dataset_lock.test_no_provider_call_during_load` 测 **Repository.load_bars**，**不是**完整回测 |
| 开发 JSON `no_provider_call_during_backtest_load` | 名实不符“完整回测” |

**当前回测不调用外部 Provider（因未接线）** → 隔离目标“意外满足”，但 **多源回测目标失败**。  
记：**no_provider_calls_during_backtest = true（现状路径）**；**多源正式设计未闭环 = false**。

---

## 8. 缓存隔离

| 层 | 结论 |
|----|------|
| `signal_cache_key` 函数 | 含 data_source/adjustment/dataset_id/weekly_bar_mode/anchor_date/execution_data_source/universe_version；单测断言 key 不同 **通过** |
| `backtest.py` 生产调用 | **不传**上述字段 → 不同源若将来同 adjust_mode，**会撞缓存（P0 设计洞）** |
| 执行缓存 | context 已加字段；req 默认空 → 隔离字段常为空串 |

验收要求“不得只检查函数参数”：本轮对 **最终生产 key 组装** 判定为 **未完成隔离**。

---

## 9. L1 / L2 价格分离

**现状（基线 cad0742 行为，本轮未改引擎核心）**:

- L1：`asof_forward_qfq`（affine-first）
- L2：raw execution / valuation

**多源映射要求**（tdxquant/front、tushare/qfq 作 L1；L2 固定 tdx_local/none）:

- **NOT IMPLEMENTED** 于 `backtest.py`
- 因此 **无法** 用 301107 做“前复权成交”回归于新路径；旧路径仍 raw 成交 → **未发现“成交用 front”的新回归**，也 **未证明** 新路径正确

`l1_l2_separation_verified`（多源场景）= **false**

---

## 10. 数据库与旧任务兼容

- `SCHEMA_SQL` / `_migrate_v1_to_v2`：6 新列 + legacy 默认值 UPDATE → **设计正确**
- `upsert_run_from_index_row` INSERT 列列表 **不含** 6 新列 → **新任务也无法写入**
- 本轮 **未** 对真实 `astock_experiments.sqlite3` 做 `PRAGMA table_info`（避免改动用户库；逻辑审查足够暴露 upsert 缺口）
- 旧任务：列添加后默认 legacy；**不会**因本 diff 把旧任务标成 tdxquant（UPDATE 条件为 NULL）
- **旧任务“损坏”**：未发现破坏性 DROP；兼容 **PARTIAL**

---

## 11. 前端人工验收

| 项 | 结果 |
|----|------|
| 信号数据源下拉 | 代码存在 |
| 周线模式 / 双源 checkbox | 代码存在 |
| dataset 详情 | **无** |
| 默认 local_aggregate | UI 默认 option 是 |
| 提交字段 | `collect…` 含 signal_data_source / weekly_bar_mode / dual_source_compare |
| 后端消费 | **无** |
| 启动系统人工截图 / 控制台 | **本轮未启动 Web**（后端未接线，UI 验收价值有限）→ **NOT VERIFIED**（交互） / **FAIL**（端到端） |

---

## 12. TdxQuant live 结果

**未通过 / 未执行正式验收**。

- 标记 `live_tdxquant` **未在 pytest.ini 注册**（`PytestUnknownMarkWarning`）
- 仅 1 个 live 测试函数，且依赖 `D:\通达信`；本轮未作为必过门禁跑通
- `tmp/tdxquant_probe` 为开发探测产物，**不能**替代正式 live 门禁

`live_tdxquant_passed = false`

---

## 13. Tushare live 结果

**无** `@pytest.mark.live_tushare` 测试文件。  
`live_tushare_passed = false`

---

## 14. 双源实验结果

**未执行真实双源实验**。  
`test_experiment_dual_source_variants.py` 仅验证：

- 手写两个 dict
- `BacktestRequest` 字段可不同  
**不**调用 `create_experiment_from_grid`，**不**跑回测。

`dual_source_experiment_verified = false`

---

## 15. 北交所与退市股票结果

- 代码与单测：`test_universe_bse.py` / `test_universe_delisted.py`（随全量 pytest 通过）
- **真实** 1 只北交所 + 2 只退市读盘：**本轮未做**
- 默认 `exclude_bj` 行为保留（需显式 include）

`bse_verified = false`（仅代码级）  
`delisted_verified = false`（仅代码级）

---

## 16. 自动化测试结果

### 16.1 默认全量（含 tmp）

```
python -m pytest -q
→ ERROR tmp/tdxquant_probe/tdx_batch_test.py collection (TQ 初始化失败)
→ Interrupted: 1 error
→ EXIT=2
```

**开发声称的 “510 passed, 0 failed” 在默认收集路径下不可复现。**

### 16.2 忽略 tmp 后

```
python -m pytest -q --ignore=tmp
→ 509 passed, 1 skipped, 5 warnings, 102.37s
→ EXIT=0
```

| 项 | 值 |
|----|-----|
| passed | 509 |
| failed | 0 |
| skipped | 1 |
| warnings | 5（含 unknown mark live_tdxquant） |
| duration | ~102s |
| 相对基线 361 | 新增约 148 项量级（509-1skip 与开发 510 接近，差 1） |

### 16.3 关键

- 原 361 在忽略 tmp 后 **未观察到失败**（全量绿）
- 新增测试多为 **DTO/存储/规范化** 测试，**大量未触及生产接线**
- **不允许**把“测试通过”等同“生产可用” — 本报告明确否决该等同

---

## 17. 性能结果

- 审计/probe 中有 TdxQuant 批量耗时记录（开发侧）
- **本轮未**跑 100 股×10 年 Repository/信号/双源基准
- `tmp/market_data_multi_source_benchmark.json` 存在但 **未作为本轮独立复测**

`performance` = **NOT VERIFIED**

---

## 18. 安全检查

| 项 | 结果 |
|----|------|
| Token 进 Git 跟踪文件 | 未发现明文 |
| 测试 token="fake" | 可接受 |
| `scripts/sync_market_data.py --token` | CLI 传入，需运维注意 shell 历史（P2） |
| 日志打印 token | 未见 |
| 目录穿越 dataset_id | 路径拼接 `manifests/{id}.json`，id 若含 `..` 有风险（P2，需校验） |
| 硬编码 `D:\通达信` | Provider/sync 默认路径；配置层味道偏业务默认（P2） |
| 裸 except | sync incremental 等有宽泛 `except Exception`（P2） |
| 生产临时 mock | 未见 |
| tmp 探测脚本进收集 | **质量问题**（P1） |

`token_leak_found = false`

---

## 19. P0 问题

### P0-1 多源字段未接入回测与实验执行路径（页面空壳）

- **等级**: P0  
- **路径**: `wtpy/apps/astock/api.py`（`BacktestBody` ~48–80 行）；`wtpy/apps/astock/service/experiments.py`（`BacktestRequest(` ~1162）；`wtpy/apps/astock/service/backtest.py`（信号加载 ~213+）  
- **描述**: UI 可选手动数据源，后端忽略；回测始终旧路径。  
- **复现**: 新建实验选“通达信前复权”或勾选双源 → 查看服务端构造的 `BacktestRequest` / 实际读数代码路径。  
- **实际**: `signal_data_source` 丢失；仍 affine/local。  
- **预期**: 按源锁定 dataset 并经 Repository 读 L1。  
- **影响**: 功能未交付；双源对照无效；结果不可按源解释。  
- **建议**: API/实验网格/runner/backtest 全链路接线；缺 dataset 时拒绝创建。

### P0-2 生产信号缓存未纳入 source/dataset_id（设计洞 / 未来必现缓存污染）

- **等级**: P0  
- **路径**: `wtpy/apps/astock/service/backtest.py` `_make_signal_cache_key` ~391–410  
- **描述**: `signal_cache_key` 已支持隔离字段，生产调用未传。  
- **复现**: 对比 `signal_cache.py` 签名与 `backtest.py` 调用实参（静态即可）。  
- **实际**: key 不含 data_source/dataset_id。  
- **预期**: 与验收要求一致纳入。  
- **影响**: 一旦多源接线但忘记改 key，将 **串用信号**。  
- **建议**: 调用点强制传入；加集成测试断言最终 key。

### P0-3 runs 表新列未在 upsert 写入 — 追溯与锁定落库失败

- **等级**: P0  
- **路径**: `wtpy/apps/astock/service/db.py` `upsert_run_from_index_row` INSERT ~330+  
- **描述**: Schema 有列，写入语句无列。  
- **复现**: 阅读 INSERT 列清单；与 SCHEMA 对比。  
- **实际**: dataset_id 等永不持久化。  
- **预期**: 新任务完整写入 6 字段。  
- **影响**: 结果页/复现/审计无法依赖 DB。  
- **建议**: 扩展 INSERT/UPDATE 与 API 响应。

### P0-4 双源对照仅测试伪造，产品路径不存在

- **等级**: P0  
- **路径**: `tests/.../test_experiment_dual_source_variants.py`；`index_v3.html` dual checkbox；`experiments.py` 无 dual_source  
- **描述**: 测试不证明产品行为。  
- **影响**: Gate 4/5 双源验收失败。  
- **建议**: 在 `expand_param_grid`/`create_experiment_from_grid` 实现双 variant，并加集成测试。

---

## 20. P1 问题

### P1-1 默认 `pytest` 收集 `tmp/` 导致失败

- **路径**: `tmp/tdxquant_probe/tdx_batch_test.py`  
- **描述**: 未 ignore 时 collection error。  
- **建议**: 移出仓库根收集路径、加 `pytest.ini` norecursedirs、或删除探测脚本出 tree。

### P1-2 无完整回测级“禁止 Provider 调用”测试

- **路径**: `test_backtest_dataset_lock.py`  
- **描述**: 只测 repo.load。  
- **建议**: 接好线后 mock 全部 Provider.fetch_bars 跑 `BacktestService.run`。

### P1-3 301107 未按真实交易周日期做 live 硬断言

- **路径**: `test_301107_tdxquant_regression.py`  
- **描述**: mock 精度 + 弱 live 搜索 open≈20.15。  
- **建议**: 固定 2026-05-11～15 日线/目标周 OHLC live 断言。

### P1-4 TdxQuant 单股失败静默丢弃

- **路径**: `tdxquant.py` `_fetch_singles`  
- **建议**: 汇总失败并 raise IncompleteResponse 或返回结构化错误供 sync 记 failed。

### P1-5 Tushare 缺 adj_factor 与真正增量/重建

- **路径**: `tushare.py`；`sync_tushare_incremental`  
- **建议**: 实现因子变更检测与全历史 QFQ rebuild。

### P1-6 Repository 允许 load partial

- **路径**: `repository.py` ~116  
- **建议**: 回测路径仅 ready；partial 仅审计工具可读。

### P1-7 live 标记未注册；无 live_tushare 套件

- **建议**: `pytest.ini` markers；补 live 测试并文档化门禁。

### P1-8 实验/API 未解析 dual_source_compare / weekly_bar_mode 贯通引擎

- **路径**: `study.py` 已支持 vendor_native；调用链未传。

### P1-9 开发测试结果 JSON 与独立复测不一致（含 tmp 时）

- **影响**: 交付可信度。

---

## 21. P2 / P3 问题

- **P2**: 硬编码 `D:\通达信`；`--token` CLI；dataset_id 字符校验；宽 `except Exception`；HTML 中文在部分 diff 显示截断（编码）。  
- **P2**: 根目录 `SZSE.39900x_d.csv` 不应入库。  
- **P3**: Parquet/Linux 同步（计划允许延后）；catalog.sqlite3 预留未用。  
- **P3**: 实施报告 Gate 状态与代码事实不符，应更正以免误导。

---

## 22. 正式上线前剩余事项（必须）

1. 将 `signal_data_source` / `dataset_id` / `weekly_bar_mode` / 执行侧 dataset **端到端**接入 API、实验、BacktestService、缓存 key、DB upsert、结果 API。  
2. 回测 L1 **仅** `MarketDataRepository.load_*`；禁止运行时 Provider。  
3. 创建任务时 resolve+锁定 ready dataset；拒绝 partial/missing。  
4. 实现真正双源 variant 扩展与结果区分。  
5. 补全 Tushare adj_factor / 增量重建；TdxQuant 失败语义。  
6. live_tdxquant + live_tushare 门禁通过并留存脱敏证据。  
7. 清理/隔离 `tmp` 探测脚本；修复默认 pytest。  
8. 100 股双源差异报告（Gate 5）。  
9. 前端 dataset 元数据展示与错误提示。  
10. 回归：忽略 tmp 全绿 + 原 361 语义 + 新集成测试（非仅 DTO）。

**非阻塞（可 CONDITIONAL 类）**: Parquet、Linux 服务器同步、非核心 UI 美化 — **当前因 P0 不满足，不能给 CONDITIONAL PASS**。

---

## 23. 是否建议合并

**否。** `merge_recommended = false`

理由：分支工作区未提交完整交付；核心验收标准（真实切换数据源、dataset 锁定、双源实验、live、生产缓存隔离）未满足。合并将把“半成品 API 表面”带入主线并制造错误安全感。

---

## 附录 A — 命令与证据索引

见 `tmp/multi_source_market_data_acceptance_commands.txt`。

## 附录 B — 机器可读摘要

见 `tmp/multi_source_market_data_acceptance.json`。
