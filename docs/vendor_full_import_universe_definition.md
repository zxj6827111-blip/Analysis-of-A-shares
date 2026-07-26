# 供应商全量导入宇宙定义 v1(Vendor Full Import Universe Definition)

| 项目 | 内容 |
|---|---|
| 状态 | 生效(规范性定义,Gate A 导入流程依据) |
| 版本 | universe_definition_version: **v1** |
| 日期 | 2026-07-26 |
| 关联 Gate | Gate A(local_vendor 全量基线导入) |
| 关联文档 | `docs/local_vendor_survivorship_bias_policy.md`、`docs/local_vendor_full_import_final_independent_acceptance.md`(P0-3)、`docs/local_vendor_import_architecture.md` |

---

## 1. 废弃"5055 只 A 股"口径的原因

原任务口径"5055 只 A 股"在独立验收中与一切实测数字均不匹配(独立验收实测,2026-07-26):

| 口径 | 独立验收实测 | 与 5055 关系 |
|---|---|---|
| 2024 截面沪深 A 股 | 5097(另北交 262) | 不等 |
| 2026 截面(A + 北交) | 5547 | 不等 |
| 2000–2026 历史并集 | 5796(A 股 5224 + 北交 571 + 302132 中航成飞) | 不等 |

废弃理由:

1. **数字不可复现**:5055 不匹配任何一个可实测口径,确认为某次未记录时点/未记录规则的截面式统计;
2. **截面 ≠ 历史并集**:全量导入的对象是 2000–2026 全部年度 ZIP 中出现过的证券,任何单一年份截面(无论 5055/5097/5547)都不是导入宇宙;
3. **固定数字必然过时**:证券随上市/退市持续变动,供应商后续补数或新年度 ZIP 到达都会改变并集。

自本文件生效起,任何任务描述、代码、验收标准不得再使用"5055"口径;宇宙一律以 §2 规则**动态计算**。

## 2. 正式定义(规范性)

**宇宙 = 供应商全部年度 ZIP 证券并集 → 分类 → 保留有效沪深 A 股 + 北交所股票**。流程四步,全部动态执行:

- **S1 枚举**:扫描供应商数据根(`E:\AStockData\raw\local_vendor\original_files\incoming`)下全部年度日 K ZIP(当前实际为 2000–2026;同一年多副本按 SHA256 去重取单副本),枚举每年出现的全部证券代码,求**并集**。年份范围取自实际存在的 ZIP 清单,不硬编码。
- **S2 分类**:对并集内每个证券,按 §3 通用代码段规则 + ZIP 内 CSV 元数据(`total_share` 等股本字段)判定 `instrument_type`(stock_a / stock_bse / etf / fund / b_share / index / bond / unknown)。
- **S3 保留**:仅保留判定为沪深 A 股与北交所股票的证券,进入导入宇宙(`inclusion_status=included`)。
- **S4 排除**:其余全部排除(`inclusion_status=excluded`)并记录 `exclusion_reason`,计入 manifest 的 `excluded_symbol_count`。

产出:`vendor_full_import_universe.csv`(§6)+ manifest 覆盖核算字段(`expected_symbol_count` 等,见 `docs/local_vendor_survivorship_bias_policy.md` §4.2)。

## 3. 纳入规则(通用代码段 + 元数据确认)

### 3.1 沪深 A 股(纳入)

| 交易所 | 代码段(通用区间) | 板块 | 说明 |
|---|---|---|---|
| SSE(沪) | 600–605 | 主板 | 6 开头主板通用区间 |
| SSE(沪) | 688–689 | 科创板 | 含 CDR/存托凭证段 689 |
| SZSE(深) | 000–004 | 主板 | 含 001/002/003/004 扩展段 |
| SZSE(深) | 30x(300–309) | 创业板 | **通用区间**,302 等新段自动覆盖 |

### 3.2 北交所(纳入)

数字代码以 **4**、**8** 开头,或 **92x**(920 段)的北交所股票(标准代码 `BSE.STK.xxxxxx` / `.BJ` 后缀)。

### 3.3 302 段与"无单票特例"原则

深交所 302 新段(实例:302132 中航成飞,独立验收实测确认在并集内)由 30x 通用区间自动覆盖。**禁止**为任何单一证券写特例(硬编码某只代码进/出宇宙);新代码段一律通过扩展通用区间规则解决,且扩展即构成规则变更,必须升 `universe_definition_version`(§5)。

注:当前 `wtpy/apps/astock/data/universe.py` 的 `is_ashare_code` 仍枚举 300/301,不识别 302(独立验收 P1 已指出)。实现必须向本定义对齐(改为 30x 通用区间),该修改属于 v1 定义的实现修正,不构成定义版本升级。

### 3.4 交易所-代码段不匹配拒绝

代码段必须与其所在交易所匹配,不匹配的记录**拒绝纳入**并记 `exclusion_reason=exchange_code_mismatch`。例如:sh 前缀 + 000 开头是上证指数(非 A 股),sz 前缀 + 399 开头是深证指数;沪市文件中出现深市专属段(或反之)一律拒绝,不做猜测性纠正。

### 3.5 equity 元数据确认

代码段规则命中后,还需以 ZIP 内 CSV 元数据做股票属性确认:`total_share`/`float_share` 等股本字段 **> 0** 方可确认为股票(equity)。股本字段缺失或为 0 的记录不能仅凭代码段纳入,按 `unknown` 处理(§4),`metadata_source` 记录判定依据。

## 4. 排除类别清单

以下类别一律排除,并逐条记录 `exclusion_reason`:

| 类别 | exclusion_reason | 典型特征 |
|---|---|---|
| ETF | `etf` | 沪 5 开头基金段、深 15/16 开头等基金代码段 |
| 场内基金/LOF 等 | `fund` | 基金代码段 + 无股本元数据 |
| B 股 | `b_share` | 沪 900、深 200 段 |
| 指数 | `index` | sh000xxx、sz399xxx 等指数段 |
| 债券/可转债 | `bond` | 债券代码段 |
| 未识别 | `unknown` | 代码段不在任何已知区间,或元数据不足以确认类型 |
| 交易所-代码段不匹配 | `exchange_code_mismatch` | §3.4 |

**未识别(unknown)不纳入**:宁可少纳入并显式记录,不做猜测性纳入。unknown 数量异常升高(如新代码段出现)应触发人工review 与定义升版本,而不是放宽运行时判定。

## 5. universe_definition_version 语义与升级规则

- `v1` 指本文件 §2–§4 的完整规则集。manifest 的 `universe_definition_version` 字段(`wtpy/apps/astock/data/dataset_store.py` `DatasetManifest`)记录构建该数据集时使用的规则版本。
- **升级规则**:任何纳入/排除规则的变更(新增代码段区间、调整元数据阈值、调整北交所口径、调整拒绝规则)必须:
  1. 升级版本号(v1 → v2 → …),旧版本文档保留不改写;
  2. 新版本规则写入本目录新文档或本文件追加版本章节;
  3. 之后构建的数据集 manifest 写入新版本号;已发布 manifest 不可变、不回改;
  4. 跨版本对比(同源数据、不同规则版本的宇宙差异)在升级说明中给出。
- 实现修正(bug fix,使代码与既有定义一致,如 §3.3 的 302 修正)不升版本,但需在变更记录中注明。
- 同一 `universe_definition_version` 下,同一批 ZIP 输入必须产出**逐字节可复现**的宇宙清单(排序稳定、去重规则确定)。

## 6. 产出物:vendor_full_import_universe.csv 字段字典

每次全量导入 preflight 阶段生成完整宇宙清单 CSV(建议落于 `<MARKET_DATA_ROOT>\reports\universe\`,并将文件 SHA256 记入当次 sync 日志),**并集内全部证券一行一条(含被排除者)**:

| 列 | 类型 | 语义 |
|---|---|---|
| `symbol` | str | 供应商原始代码(如 `sh600000` / `sz302132` / `bj920008` 形态,保留原样) |
| `canonical_symbol` | str | 标准化代码(`SSE.STK.600000` / `SZSE.STK.302132` / `BSE.STK.xxxxxx`),规则见 `universe.py::to_std_code` |
| `exchange` | str | `SSE` / `SZSE` / `BSE` |
| `board` | str | `main`(主板)/ `gem`(创业板 30x)/ `star`(科创板 688–689)/ `bse`(北交所) |
| `instrument_type` | str | `stock_a` / `stock_bse` / `etf` / `fund` / `b_share` / `index` / `bond` / `unknown` |
| `first_seen_date` | int(YYYYMMDD) | 该证券在全部年度 ZIP 中出现的最早行情日期 |
| `last_seen_date` | int(YYYYMMDD) | 最晚行情日期 |
| `first_seen_year` | int | 首次出现的年度 ZIP 年份 |
| `last_seen_year` | int | 最后出现的年度 ZIP 年份 |
| `present_in_latest_year` | bool | 是否出现在最新年度 ZIP(当前 2026)中;false 即"近似已退市/停牌消失"信号 |
| `source_archive_count` | int | 该证券出现于多少个年度 ZIP(去重后) |
| `source_row_count` | int | 全年度合计原始行数(跨 ZIP 重复日期去重前的原始计数,供质量核对) |
| `inclusion_status` | str | `included` / `excluded` |
| `inclusion_reason` | str | 纳入依据,如 `ashare_code_segment+equity_metadata` / `bse_code_segment+equity_metadata`;excluded 行为空 |
| `exclusion_reason` | str | §4 枚举值;included 行为空 |
| `known_delisted` | bool | 是否已知退市。基于 `present_in_latest_year=false` 等供应商侧信号的**近似判定**;供应商数据无法识别"从未被收录的退市股"(幸存者偏差,见政策文档) |
| `survivorship_risk` | bool | 幸存者风险标记。v1 下 local_vendor 来源全部为 `true`(整个清单继承数据源级偏差);保留此列供 Gate B 复合宇宙按来源区分 |
| `metadata_source` | str | 类型判定依据,如 `vendor_csv_metadata`(股本字段)/ `code_segment_only` / `code_segment+vendor_csv_metadata` |

约束:CSV 行数、`included` 计数等汇总值必须与 manifest 的 `expected_symbol_count` / `excluded_symbol_count` 一致,验收时交叉核对。

## 7. 禁止硬编码宇宙数量(强制条款)

- 任何代码(导入、校验、回测、前端、测试)**不得**硬编码宇宙数量(5055/5097/5547/5796/5224 等一律禁止作为程序常量或断言精确值);
- 宇宙数量只能来自当次动态计算结果(S1–S4),对外展示时须同时展示 `universe_definition_version`、计算时间与输入 ZIP 年份范围;
- 文档与报告引用具体数字时,必须标注口径与来源(如"独立验收实测,2026-07-26");
- 测试如需数量断言,只允许区间/不变量式断言(如 `expected = imported + no_data + failed`、并集 ≥ 任意单年截面),不允许精确数断言。

## 8. 参考实现与文件路径

| 用途 | 路径 |
|---|---|
| 年度 ZIP 枚举/读取 | `wtpy/apps/astock/data/providers/local_vendor.py`(`LocalVendorProvider.available_years` / `list_symbols_in_year` / `fetch_universe`) |
| 代码段/标准化规则 | `wtpy/apps/astock/data/universe.py`(`is_ashare_code` / `is_bse_code` / `to_std_code`;302 修正见 §3.3 注) |
| 全量导入入口 | `scripts/sync_market_data.py`(`sync_local_vendor_full`,`--source local_vendor --mode full`) |
| manifest 覆盖核算 | `wtpy/apps/astock/data/dataset_store.py`(`DatasetManifest` / `evaluate_strict_publish`) |
| 独立验收宇宙清单(本定义的实测参照) | `tmp/local_vendor_full_import_universe.csv` |
| 供应商原始数据根 | `E:\AStockData\raw\local_vendor\original_files\incoming` |
