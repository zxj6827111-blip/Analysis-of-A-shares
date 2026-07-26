# Tushare 退市股补充与复合执行数据集设计(Composite Execution Dataset Plan)

| 项目 | 内容 |
|---|---|
| 状态 | 设计稿(**下一阶段执行,不在本轮范围**) |
| 版本 | v0.1(design draft) |
| 日期 | 2026-07-26 |
| 关联 Gate | Gate B(退市股补齐与复合数据集验收) |
| 关联文档 | `docs/local_vendor_survivorship_bias_policy.md`(Gate A 政策)、`docs/vendor_full_import_universe_definition.md`(宇宙 v1)、`docs/local_vendor_import_architecture.md` |

---

## 1. 目标与范围

**目标**:用 Tushare 补充供应商缺失的历史退市股日线,合并产出**单一**复合执行数据集,使长期全市场回测的幸存者偏差可度量地收敛,并按实测证据更新偏差标记。

**前置事实**:

- 供应商数据(local_vendor)存在幸存者偏差,历史退市股缺失(独立验收实测,证据样本 300104/002680/601558/002450/600001/600002 在全部年度 ZIP 中不存在;详见政策文档);
- 开发侧已验证 Tushare 有 **338 只退市股含日线与复权因子**(证据:`tmp/blocker_fix/delisted_verification.json`;其中含 3 只逐条深度探测样本 002898.SZ/000004.SZ/002808.SZ 的 daily/adj_factor 行数;另记录 000003 因探测区间 2010–2024 早于其退市年份未取到数据,提示早年退市股需全量逐只核实,见 §7);
- 现行数据集架构:`DatasetStore` 内容寻址 blob(sha256 npz)+ 不可变 manifest,状态 building → ready/partial/failed;回测只锁一个 `execution_dataset_id`,partial/building 不可回测,回测期 Provider 调用 = 0(`wtpy/apps/astock/data/dataset_store.py`,相关不变量测试:`tests/apps/astock/test_backtest_dataset_lock.py`、`test_provider_no_silent_fallback.py`、`test_execution_cache_dataset_isolation.py`、`test_dataset_atomic_publish.py`)。

**范围外(本轮明确不做)**:本文件仅为设计与实施计划,本轮不实现代码、不拉取数据、不发布数据集。

## 2. 三层架构

```
L1  local_vendor / none            供应商可得历史(vendor_available_historical_union)
    dataset_id 形如 local_vendor_none_1d_<cutoff>_<sha12>
        │
L2  tushare / delisted_none        仅供应商缺失的退市股,未复权日线
    (建议独立 source 键 tushare_delisted,与既有 tushare/qfq 信号数据集隔离)
        │
        ▼  合并器(composite builder,sync 期离线执行)
L3  internal / composite_none      合并后的单一执行数据集
    parent_dataset_ids = [L1_id, L2_id],回测仅锁 L3 的 execution_dataset_id
```

| 层 | source/adjustment | 内容 | 角色 |
|---|---|---|---|
| L1 | `local_vendor` / `none` | 供应商全量基线(宇宙定义 v1) | 存续股票 + 供应商已含的近期退市股 |
| L2 | `tushare_delisted`(建议)/ `none` | **仅** L1 缺失的退市股,未复权日线 | 缺口补充,永不含 L1 已有股票 |
| L3 | `internal_composite`(建议)/ `none` | L1 ∪ L2 合并结果 | 唯一对回测暴露的执行数据集 |

命名说明:L2 采用独立 source 键(而非复用 `tushare`)是为了让 `resolve_latest_ready(source, adjustment)` 不与既有 tushare/qfq 信号数据集产生歧义;L3 同理独立于一切 Provider 源。最终 source 字符串以实现评审为准,但**必须**保证三层 dataset 族互不混淆。

## 3. 设计约束(强制)

- **C-1 单执行数据集不变量**:回测仍只锁一个 `execution_dataset_id`(即 L3);运行时**不得**同时读两个 execution dataset。L1/L2 仅在 sync/合并期被读取,回测期 Provider 调用 = 0 的既有不变量对 L3 同样成立。
- **C-2 每股 provenance**:L3 manifest 对每只股票记录来源 `provenance ∈ {vendor, tushare}` 及其来源 dataset_id(`SymbolRecord` 需扩展 provenance 字段——schema 变更点)。
- **C-3 parent_dataset_ids**:L3 manifest 记录 `parent_dataset_ids=[L1_id, L2_id]`(现 `DatasetManifest.parent_dataset_id` 为单值 Optional,需扩展为列表或新增复数字段——schema 变更点)。
- **C-4 同一股票不混拼两来源**:供应商有该股 → 整段历史全用供应商;供应商完全缺失 → 整段历史全用 Tushare。**禁止**同一股票按日期段拼接两来源。
- **C-5 重叠数据先做一致性验证**:对两源都有数据的抽样股票,合并前必须做价格/量级抽样对比(单位归一后 OHLC 相对误差、成交量/成交额量级、日期集合差异),不通过则 Gate B fail。重叠数据仅用于校验,不参与来源选择(C-4 下 vendor 优先)。
- **C-6 合并规则版本化**:合并规则写入 `composite_rule_version`(如 `cr-v1`),规则变更必须升版本;manifest_sha 覆盖该字段,保证同输入同规则可复现。
- **C-7 残余缺失显式记录**:合并后仍缺失的股票(Tushare 也无数据者)在 L3 manifest 的 `known_missing_delisted_count/symbols`(或扩展的 residual 字段)显式记录,禁止静默丢弃。
- **C-8 偏差标记按实测评估**:L3 的 `survivorship_bias / historical_universe_complete / delisted_coverage_complete` 由 Gate B 独立验收按缺口核算评估,不自动置 true/false(政策文档 PD-5)。
- **C-9 单位归一**:L2/L3 落库单位与 L1 一致——volume 以股、amount 以元。供应商侧换算已实证:volume 手→股 ×100,amount 千元→元 ×1000(独立验收 VWAP 实证 400/400);Tushare 侧名义单位(vol 手、amount 千元)必须用同一 VWAP 方法独立实证后方可采用,不得凭接口文档直接换算入库。
- **C-10 blob 复用**:L1/L2/L3 共用同一 DatasetStore 根时,L3 直接引用 L1/L2 已有 blob 的 sha256,不复制字节;`publish()` 的 blob 存在性校验(缺 blob → failed)天然覆盖 L3。

## 4. L3 manifest 扩展字段(设计)

在 Gate A 字段(见政策文档 §4)基础上新增:

| 字段 | 类型 | 含义 |
|---|---|---|
| `parent_dataset_ids` | list[str] | `[L1_dataset_id, L2_dataset_id]` |
| `composite_rule_version` | str | 合并规则版本(初始 `cr-v1` = 本文件 §3 C-4/C-5 规则) |
| `symbols[].provenance` | str | `vendor` / `tushare` |
| `provenance_counts` | dict | 如 `{"vendor": N1, "tushare": N2}`,与 symbol 级记录交叉核对 |
| `overlap_check` | dict | 一致性验证摘要:抽样数、通过数、阈值、报告文件路径 |
| `residual_missing_symbols` / `_count` | list/int | 两源合并后仍缺失的股票(显式记录) |
| `universe_type` | str | 建议 `vendor_union_plus_tushare_delisted`(区别于 L1 的 `vendor_available_historical_union`),命名随实现评审定稿并纳入 `universe_definition_version` 升级流程 |

## 5. 实施步骤清单(编号,含验收点)

> 涉及文件均为建议落点;执行时如有调整,须在 Gate B 验收材料中说明。

- **S1 确定缺失清单**
  - 内容:以宇宙定义 v1 的 L1 并集清单(`vendor_full_import_universe.csv`)为基,对照 Tushare `stock_basic`(上市 + 退市全状态)求差集,产出 `missing_delisted_symbols.csv`(含 ts_code、上市/退市日期、与 L1 known_delisted 的交叉标记)。
  - 涉及:`wtpy/apps/astock/data/providers/tushare.py`(`fetch_universe`,需确认覆盖 `list_status='D'`)、新脚本或 `scripts/sync_market_data.py` 子命令。
  - 验收点:清单可复现(同日重跑一致);政策文档 6 只证据样本股全部出现在缺失清单中;338 只已验证清单(`tmp/blocker_fix/delisted_verification.json`)与本清单的差异有逐条解释。
- **S2 Tushare 全量拉取 none 日线**
  - 内容:对缺失清单逐只拉取全生命周期(上市日→退市日)未复权日线;复权因子一并拉取存档(不参与 L3 none 合并,见 §7)。
  - 涉及:`wtpy/apps/astock/data/providers/tushare.py`(复用 `_fetch_raw_daily` / `fetch_adj_factor` / `_call_with_retry` 限频重试)、`scripts/sync_market_data.py` 新增 source(如 `--source tushare_delisted`,复用 `_sync_dataset` 骨架)。
  - 验收点:每只股票请求区间 = [list_date, delist_date](吸取 000003 探测区间教训);拉取行数与 Tushare 返回逐只对账;失败/无数据逐只记录,不静默跳过。
- **S3 单位/质量校验**
  - 内容:复刻既有 VWAP 单位实证方法(抽样股票 × 交易日,VWAP 必须落在当日高低区间)验证 Tushare 的 vol/amount 单位;OHLC 合法性(high ≥ max(open,close) 等)、日期升序无重复、与交易日历比对缺口。
  - 涉及:`tmp/` 校验脚本(参照 `tmp/blocker_fix/security_types_units.py` 方法论);校验报告落 `E:\AStockData\reports\`。
  - 验收点:单位实证样本全数通过(方法与供应商侧 400/400 实证同构);异常样本有逐条结论。
- **S4 发布 tushare_delisted_none 数据集(L2)**
  - 内容:经 DatasetStore 落 blob + manifest,状态机 building → ready/partial/failed 与严格发布核算(expected/imported/no_data/failed)全套沿用。
  - 涉及:`scripts/sync_market_data.py`、`wtpy/apps/astock/data/dataset_store.py`(如需仅为 L2 增加 universe_type 取值,不改核心逻辑)。
  - 验收点:manifest 字段齐全(含 Gate A 偏差字段的 L2 适配表述);中断重跑演练通过(无 .tmp 残留、旧 ready 不受损、blob 内容寻址复用)。
- **S5 合并器实现**
  - 内容:新建 `wtpy/apps/astock/data/composite.py`:输入两个 ready 的 parent manifest,按 C-4 规则选源、按 C-10 复用 blob、执行 C-5 重叠抽检,输出 L3 manifest 草稿。合并为纯离线 sync 期操作,不引入任何回测期读取路径。
  - 涉及:`wtpy/apps/astock/data/composite.py`(新建)、`wtpy/apps/astock/data/dataset_store.py`(C-2/C-3 schema 扩展)、`scripts/sync_market_data.py`(如 `--source composite --parents <L1_id>,<L2_id>`)。
  - 验收点:parent 任一非 ready → 拒绝合并;同输入同规则重跑产出相同 manifest_sha;单元测试覆盖 C-4(不混拼)、C-7(残余缺失记录)。
- **S6 composite manifest 发布(L3)**
  - 内容:写入 `parent_dataset_ids`、per-symbol `provenance`、`composite_rule_version`、`overlap_check`、`residual_missing_*`,经 `publish()` 严格校验后发布。
  - 验收点:manifest 逐字段核对;`provenance_counts` 与 symbol 级记录一致;引用 blob 全部存在;L1/L2 manifest 未被改写(不可变)。
- **S7 隔离回测验证**
  - 内容:以 L3 为 execution dataset 跑真实产品路径回测。
  - 验收点:回测期 Provider 调用 = 0(沿用既有计数法);全程仅打开 L3 一个 execution dataset(C-1);同参数下 L1 与 L3 各跑一次 → run_id 不同且落库 execution_dataset_id 正确;对 provenance=vendor 的股票,L3 回测结果与 L1-only 回测逐字段一致(合并不改变供应商数据);partial/building 状态的 L3 被 400 拒绝。
  - 涉及:`tests/apps/astock/` 新增 composite 用例,复用 `test_backtest_dataset_lock.py` / `test_execution_cache_dataset_isolation.py` 模式。
- **S8 Gate B 验收**
  - 内容:独立验收缺口核算(应补退市股总数的口径论证 vs 实际补齐数 vs 残余缺失数)、偏差字段评估(C-8)、警告文案更新或保留的决策、全部 S1–S7 验收点复核。
  - 验收点:出具独立验收报告;L3 的 recommended/prohibited use 依据实测重写;若仍有缺口,警告文案保留并注明残余缺失规模来源。

## 6. 与既有系统的接口影响(预估)

- 回测服务:`wtpy/apps/astock/service/backtest.py` / `backtest_request.py` 的 execution 源枚举需接纳 L3 source 键,校验逻辑(仅 ready 可回测、单数据集锁定)不变;
- 前端/实验中心:数据集选择与展示需呈现 L3 的 provenance 统计与警告文案(政策文档 §5 的展示义务同样适用于 L3);
- 信号数据集(tdxquant/tushare qfq)不受本计划影响,信号/执行双源结构不变。

## 7. 风险与开放问题

| 编号 | 风险/开放问题 | 说明与初步对策 |
|---|---|---|
| R-1 | Tushare 积分/限频 | 退市股全量拉取(338+ 只 × 全生命周期日线 + 复权因子)受积分与每分钟调用数限制;`_call_with_retry` 已有退避重试,仍需预算调用量、支持断点续拉(S2 逐只落盘)。 |
| R-2 | 退市股复权因子口径 | L2/L3 以 none(未复权)为准,复权因子仅拉取存档。退市股因子与现行前复权仿射模型(commit cad0742,affine forward-adjustment)如何衔接、是否为退市股提供 qfq 视图,为开放问题,不阻塞 none 合并。 |
| R-3 | 北交所退市覆盖 | Tushare 对北交所退市股的覆盖范围未验证(338 只清单的交易所构成未核算);Gate B 缺口核算需单列北交所口径,必要时接受"北交所退市不完整"并在 L3 manifest 显式记录。 |
| R-4 | 与 signal 数据集的日历对齐 | 执行侧交易日历当前依赖 calendar.json / TDX sh000001.day(独立验收已指出其在 dataset 之外);退市股在退市日之后无行情,信号/执行日历需明确"股票生命周期 ⊂ 市场日历"的处理,避免持仓跨退市日的估值缺口。需在 S7 用含退市股的组合回测专项验证。 |
| R-5 | 早年退市股数据可得性 | `delisted_verification.json` 中 000003 因探测区间(2010–2024)未取到数据,说明探测方法必须按 [list_date, delist_date] 定区间;早年(2000 年前后)退市股在 Tushare 的真实覆盖率未知,可能构成 residual_missing 的主体。 |
| R-6 | "应补总数"口径 | 338 是"Tushare 可得"清单,不是历史退市 A 股总数;Gate B 需给出独立口径(如交易所公开退市名单)评估 L3 完整度,否则 `delisted_coverage_complete` 无法诚实评估。 |
| R-7 | 单位实证 | Tushare vol/amount 单位不得凭文档采信(C-9),须复刻 VWAP 实证;若北交所/早年数据单位存在口径差异,须分层抽样。 |

## 8. 本轮明确不做

- 不实现 `composite.py`、不改 `dataset_store.py` schema、不新增 sync source;
- 不拉取任何 Tushare 数据、不发布 L2/L3 数据集;
- 不修改回测服务与前端;
- 本文件仅作为 Gate B 的设计输入,实施前需评审定稿(source 命名、schema 扩展方式、S1 差集口径)。
