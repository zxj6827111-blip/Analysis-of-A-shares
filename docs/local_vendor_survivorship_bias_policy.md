# 供应商数据幸存者偏差政策(Survivorship Bias Policy for local_vendor)

| 项目 | 内容 |
|---|---|
| 状态 | 生效(Gate A 决策文档) |
| 版本 | v1.0 |
| 日期 | 2026-07-26 |
| 关联 Gate | Gate A(带偏差标记的全量基线导入);解除条件见 Gate B(§7) |
| 关联文档 | `docs/vendor_full_import_universe_definition.md`、`docs/tushare_delisted_composite_dataset_plan.md`、`docs/local_vendor_full_import_final_independent_acceptance.md`(P0-2 原始发现) |

---

## 1. 问题陈述

供应商日 K 数据(`E:\AStockData\raw\local_vendor\original_files\incoming`,2000–2026 年度 ZIP)不是真实时点(point-in-time)的历史全市场数据,而是按"约 2025 年仍上市的股票清单"**回填**出来的历史序列。历史上真实存在、后来退市的大量股票在全部年度 ZIP 中缺失。

直接后果:用该数据做长期(尤其是跨越退市高发期的)全市场回测,样本天然剔除了失败者,收益、胜率、回撤等统计量存在系统性向好偏差(幸存者偏差,survivorship bias)。该缺陷是**数据源本身的缺陷**,无法通过工程手段在系统内修复,只能通过补充数据源(Gate B)或更换数据源解决。

## 2. 证据(独立验收实测)

以下事实全部来自 2026-07-26 独立验收实测(详见 `docs/local_vendor_full_import_final_independent_acceptance.md` P0-2/P0-3,及 `tmp/local_vendor_full_import_universe.csv`):

1. **知名历史退市股全部缺失。** 乐视网 300104、长生生物 002680、华锐风电 601558、康得新 002450、邯郸钢铁 600001、齐鲁石化 600002,在 2000–2026 全部年度 ZIP 中均不存在(其中乐视网 2010–2020 实际交易十年,任何年份 ZIP 均无该股)。此 6 只为**证据样本,非穷举**——真实缺失的退市股数量远大于此(参考:Tushare 侧已验证 338 只退市股可得日线与复权因子,见 §7)。
2. **早年成分几乎原样存续至今。** 2000.zip 共 873 只证券,其中 870 只至今仍在 2026.zip 中。若为真实时点数据,26 年间不可能仅 3 只退出。
3. **历史并集 ≈ 最新截面。** 2000–2026 全年度 ZIP 的 A 股并集与 2026 截面 A 股仅相差 6 只(000627/000851/300280/600200/601989/603388),且全部为 2025–2026 年**近期**退市股。即数据只"记得"最近的退市,不含更早的退市历史。
4. **规模数字(独立验收实测,仅作背景引用,正式流程必须动态计算,禁止硬编码):**

| 口径 | 数量 |
|---|---|
| 2024 截面沪深 A 股 | 5097(另北交 262) |
| 2026 截面(A + 北交) | 5547 |
| 2000–2026 历史并集 | 5796(A 股 5224 + 北交 571 + 302132 中航成飞) |

结论:该数据 ≈ "约 2025 年仍上市股票"的回填历史,不构成完整历史宇宙。

## 3. 政策决定

- **PD-1(允许导入)**:允许基于该数据建立 `universe_type = vendor_available_historical_union`(供应商可得历史并集)的全量执行基线数据集并投入使用。P0-2 不再阻断全量导入,偏差改由"显式标记 + 用途约束"管理。
- **PD-2(强制标记)**:该类数据集的 manifest **必须**携带 §4 全部偏差与覆盖字段。缺失这些字段的 local_vendor 全量 manifest 视为不合规,不得作为正式执行数据集使用。
- **PD-3(强制警示)**:所有展示该数据集或其回测结果的页面/报告/API 消费端,必须显示 §5 警告文案。
- **PD-4(用途约束随数据分发)**:`recommended_use` 与 `prohibited_or_discouraged_use` 写入 manifest 本身(§6),约束随数据集流转,不依赖读者是否读过本文档。
- **PD-5(禁止自我升格)**:任何流程不得将本数据源单独产出的数据集标记为 `survivorship_bias=false`、`historical_universe_complete=true` 或 `delisted_coverage_complete=true`。这些标志只能在 Gate B 复合数据集上、依据新一轮独立验收的实测证据评估(§7)。

## 4. manifest 必带字段清单

Gate A 后,`DatasetManifest`(`wtpy/apps/astock/data/dataset_store.py`)新增以下字段,local_vendor 全量数据集必须全部填写:

### 4.1 宇宙与偏差标记

| 字段 | 类型 | 本数据源取值 | 含义 |
|---|---|---|---|
| `universe_type` | str | `vendor_available_historical_union` | 宇宙口径:供应商全部年度 ZIP 证券并集经分类过滤后的集合,**不是**真实历史全市场股票池 |
| `survivorship_bias` | bool | `true` | 数据集存在幸存者偏差(历史退市股缺失) |
| `historical_universe_complete` | bool | `false` | 历史宇宙不完整:历史上真实存在过的股票未被全部覆盖 |
| `delisted_coverage_complete` | bool | `false` | 退市股覆盖不完整:仅含 2025–2026 近期退市的极少数股票 |
| `coverage_start_year` | int | 动态(当前实际为 2000) | 年度 ZIP 覆盖起始年,取自实际导入的 ZIP 清单,不硬编码 |
| `coverage_end_year` | int | 动态(当前实际为 2026) | 年度 ZIP 覆盖截止年,同上 |
| `known_missing_delisted_count` | int | 当前证据样本为 6 | **已证实**缺失的退市股数量。是证据样本计数,非缺失总数;禁止对外解读为"只缺 N 只" |
| `known_missing_delisted_symbols` | list[str] | `300104/002680/601558/002450/600001/600002` | 已证实缺失的退市股清单(证据样本,非穷举) |
| `universe_definition_version` | str | `v1` | 宇宙定义规则版本,指向 `docs/vendor_full_import_universe_definition.md`;规则变更必须升版本 |
| `warning_text` | str | §5 固定文案 | 展示层必须原样透出的警告文案 |
| `recommended_use` | list[str] | §6.1 清单 | 允许/推荐用途 |
| `prohibited_or_discouraged_use` | list[str] | §6.2 清单 | 禁止/不推荐用途 |

### 4.2 覆盖核算字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `expected_symbol_count` | int | 按宇宙定义 v1 动态计算出的应导入证券数 |
| `imported_symbol_count` | int | 实际成功导入(有行情数据落 blob)的证券数 |
| `excluded_symbol_count` | int | 被分类规则排除(ETF/基金/B 股/指数/债券/未识别等)的证券数 |
| `no_data_symbol_count` | int | 属于宇宙但源数据中无有效行情的证券数 |
| `failed_symbol_count` | int | 拉取/解析失败的证券数 |
| `warning_symbol_count` | int | 带质量警告但仍纳入的证券数 |
| `coverage_ratio` | float | `imported_symbol_count / expected_symbol_count`,ready/partial 判定与验收核对用 |
| `no_data_allowlist` | list | 允许 no_data 不阻断 ready 的白名单及逐条理由(回应独立验收"no_data 容忍策略需明确"的警告) |

核算恒等式(发布时校验):`expected = imported + no_data + failed`(excluded 在 expected 之外单独计数)。

## 5. 展示层警告文案(强制)

manifest `warning_text` 固定为:

> **该数据集缺少部分历史退市股票,长期全市场回测存在幸存者偏差。**

要求:

1. 数据仓库页(`wtpy/apps/astock/web/static/index_v3.html` 数据仓库视图)在该类数据集条目上显示此文案;
2. 使用该数据集作为执行数据集的回测/实验结果页与导出报告,必须携带此文案及 `execution_dataset_id`;
3. `/api/v1/market-data/status` 等 API 返回该数据集时透出 `warning_text` 原文,由前端原样渲染,不得改写弱化;
4. 文案不得被配置项关闭。

## 6. 用途约束

### 6.1 recommended_use(推荐/允许用途)

- 导入流程本身的工程验证(全量导入、原子发布、中断恢复、内容寻址复用等链路演练);
- 技术指标/卦象等信号形态研究(单票或组合层面,不以全市场长期收益统计为结论);
- 当前存续股票(present_in_latest_year=true)的研究与回测;
- 作为工程基线数据集:执行缓存隔离、数据集锁定、性能与容量基准;
- 与后续完整数据集(Gate B 复合数据集)做对照实验,量化幸存者偏差的影响幅度。

### 6.2 prohibited_or_discouraged_use(禁止/不推荐用途)

- **禁止**宣称基于该数据集得到"完整 27 年全市场"收益/胜率/风险统计;
- **禁止**宣称该数据集"无幸存者偏差"或以任何表述暗示历史覆盖完整;
- **禁止**将该数据集的历史成分宣称为"历史上真实可交易的股票池"(它是 2025 年前后存续股票的回填集合);
- 不推荐:任何跨越大规模退市周期、且结论依赖全市场横截面统计的长期策略评估——如确需进行,报告必须显著携带 §5 文案并注明 `universe_type`。

## 7. 与 Gate B(补齐退市股后)的关系

- **Gate A(本轮)**:接受偏差、显式标记、完成 local_vendor 全量基线导入。本政策即 Gate A 的用户决策落地(对应独立验收 P0-2 要求的"决策 ① 接受偏差并显著标注",并叠加决策 ② 作为下一阶段)。
- **Gate B(下一阶段,设计见 `docs/tushare_delisted_composite_dataset_plan.md`)**:用 Tushare 补充供应商缺失的退市股(开发侧已验证 Tushare 有 338 只退市股含日线与复权因子,证据:`tmp/blocker_fix/delisted_verification.json`),合并产出单一复合执行数据集 `internal/composite_none`。
- 字段翻转规则:
  - 复合数据集的 `survivorship_bias / historical_universe_complete / delisted_coverage_complete` **不自动**变为无偏/完整,必须依据 Gate B 独立验收对"应有退市股总量 vs 实际补齐量"的缺口核算评估。Tushare 的 338 只是"Tushare 可得清单",不等于历史退市 A 股总数,后者本轮未穷举验证;
  - local_vendor 单源数据集(L1)的偏差字段**永久保持** `survivorship_bias=true`,不因 Gate B 完成而改写(不可变 manifest 语义本身也不允许);
  - 若 Gate B 后仍有缺口,复合数据集延续 `known_missing_delisted_*` 机制显式记录残余缺失。
- 在 Gate B 完成并通过独立验收之前,本政策对所有 local_vendor 执行数据集持续有效。

## 8. 证据与实现文件索引

| 类别 | 路径 |
|---|---|
| 独立验收报告(P0-2/P0-3 原始发现) | `docs/local_vendor_full_import_final_independent_acceptance.md` |
| 并集宇宙清单(5796 行,独立验收产物) | `tmp/local_vendor_full_import_universe.csv` |
| Tushare 退市股验证证据 | `tmp/blocker_fix/delisted_verification.json` |
| manifest 字段实现 | `wtpy/apps/astock/data/dataset_store.py`(`DatasetManifest`) |
| 供应商 Provider | `wtpy/apps/astock/data/providers/local_vendor.py` |
| 全量导入入口 | `scripts/sync_market_data.py`(`--source local_vendor --mode full`) |
| 供应商原始数据根 | `E:\AStockData\raw\local_vendor\original_files\incoming` |
