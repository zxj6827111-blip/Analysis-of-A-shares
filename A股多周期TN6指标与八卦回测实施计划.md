# A 股多周期、TN6 指标与八卦回测实施计划

## 1. 目标与结论

本项目以 WonderTrader 为底层回测引擎，建设一套面向沪深 A 股的本地回测扩展，完成以下能力：

- 只读导入 `D:\通达信` 的沪深 A 股日线数据。
- 支持日线、周线、月线分别计算和回测，并支持三周期共振。
- 建立统一指标注册中心，支持导入和选择通达信 `.tn6` 指标。
- 将 `指标` 目录内现有 `.tn6` 作为第一批系统默认指标。
- 将八卦 OHLC 计算作为可选的原生指标，纳入同一指标选择体系。
- 提供 Python API、CLI、CSV/Excel 报告和 WonderTrader 组合回测，第一版不开发 Web 页面。

当前 WonderTrader 核心能力可以承载该需求，但项目目前缺少完整的数据转换、指标导入、公式执行、多周期聚合、八卦知识库和报告链路，因此现状不能直接开展正式全市场回测。

## 2. 已确认的本地事实

- 通达信根目录为 `D:\通达信`。
- 日线位于 `vipdoc\sh\lday`、`vipdoc\sz\lday` 和 `vipdoc\bj\lday`。
- 本地约有 9,427 个 `.day` 文件，约 0.474 GB，样本主要覆盖 2016-01-04 至 2026-07-17。
- 第一版只覆盖沪深 A 股，不包含北交所。
- 本地一分钟和五分钟数据历史过短，暂不满足长期 `MIN60` 回测。
- 本项目股票合约资料和节假日文件已过期，不能直接作为完整股票池和交易日历。
- `WtBtPorter.dll` 可加载，WonderTrader 支持外部数据、DSB、股票 T+1 和组合回测。
- `指标` 目录当前包含 4 个 `.tn6`、3 个公式文本文件和 1 个八卦 Excel。
- 三个“双增多周期共振” `.tn6` 的 SHA256 完全相同，应作为一个内容版本、三个名称别名处理。
- `.tn6` 是通达信二进制/加密导出包，不是可以直接读取的公式文本。
- `735金叉及趋势.tn6` 当前没有配套公式源码，不能假设或伪造其计算逻辑。
- 八卦 Excel 工作表范围为 `A1:H385`，包含 64 卦、384 爻，每卦恰好 6 爻。

## 3. 已锁定的业务规则

### 3.1 周期与指标

- 日、周、月分别计算、出信号、统计和回测。
- “多周期共振选股 V5”在日、周、月分别运行。
- DWM 共振定义为：日线最终选股条件成立，同时最近已经闭合的周线和月线最终条件成立。
- 周线和月线只能使用已闭合周期；普通交易日沿用最近闭合周期状态，周/月最后交易日收盘后才允许使用刚闭合的新状态。
- “先跌后涨完整版”保持原始“日线 + 60 分钟”含义；分钟数据补齐前显示为数据不足并禁止正式回测，不得擅自改成周线或月线。

### 3.2 TN6 导入边界

采用“稳定混合导入”方案：

- 系统接收、登记、去重、版本管理和展示 `.tn6`。
- 系统不逆向破解 `.tn6`，实际执行使用与 `.tn6` 明确配对的通达信公式源码。
- 不得仅依据文件名相似度自动建立 `.tn6` 与 `.txt` 的执行映射。
- 未配对源码的指标可以进入指标目录，但状态必须是 `source_required`，不得进入正式回测。
- 取得源码后，保存源码 SHA256 和 `.tn6` SHA256，映射关系可追溯。

### 3.3 八卦指标

- 八卦使用未复权、固定两位小数的真实 OHLC。
- 对格式化后的价格数字逐位求和，例如 `5.90` 计算为 `5+9+0`；价格超过 9.99 时仍计算全部数字。
- 开盘数字和除以 8 的余数决定上卦；收盘决定下卦；余数 0 映射为 8=地/坤。
- 最高价与最低价的数字和再次相加，除以 6 的余数决定动爻；余数 0 映射为第六爻。
- 爻序按自下向上 1 至 6，并从 Excel 查询实际爻名、爻辞和行情简判。
- 第一版八卦只提供分类、解释和统计验证，不将古文主观转换为买卖信号。
- 八卦单独选择时对每根完整周期 K 线做条件收益研究；与选股指标同时选择时，为选股信号附加卦象标签。

### 3.4 组合回测

- 信号在对应周期收盘后确认，下一可交易日开盘买入。
- 日、周、月分别运行持有 1、3、5 个对应周期的组合。
- DWM 共振按 1、3、5 个交易日运行。
- 重复信号不重置已有持仓的持有期。
- 初始资金默认 1,000,000 元，可配置。
- 符合条件股票等权，单票权重上限默认 10%，按 100 股整数手下单。
- 停牌、没有有效次日行情或无法形成有效开盘价时取消该次入场。
- 卖出遇不可成交的跌停状态时顺延至可成交日。
- 佣金、最低佣金、印花税和滑点全部配置化，示例值不得表述为用户真实成本。

## 4. 推荐架构

在 `wtpy/apps/astock/` 建立独立扩展，不修改 WonderTrader 核心语义：

```text
wtpy/apps/astock/
  __init__.py
  __main__.py
  cli.py
  config.py
  data/
    tdx_reader.py
    data_store.py
    universe.py
    calendar.py
    adjustments.py
    periods.py
  indicators/
    models.py
    registry.py
    tn6_importer.py
    parser.py
    ast_nodes.py
    compiler.py
    runtime.py
    builtins.py
  bagua/
    calculator.py
    knowledge.py
    bagua_384.json
  study.py
  strategy.py
  reports.py
```

测试集中放在 `tests/apps/astock/`，生成数据放在 `storage/astock/`，每次运行输出到 `outputs/astock/<run_id>/`。不得写入或修改 `D:\通达信`。

## 5. 核心接口

### 5.1 指标定义

```python
IndicatorSpec(
    id: str,
    name: str,
    version: str,
    kind: Literal["tdx_formula", "native"],
    output_type: Literal["signal", "series", "classification"],
    supported_periods: tuple[str, ...],
    source_file: str | None,
    source_sha256: str | None,
    package_file: str | None,
    package_sha256: str | None,
    compile_status: Literal[
        "ready", "source_required", "unsupported", "invalid"
    ],
    parameters: dict,
)
```

### 5.2 指标选择行为

| 指标类型 | 输出 | 回测行为 |
|---|---|---|
| 通达信选股公式 `XG:` | 布尔信号 | 可以运行组合回测 |
| 通达信趋势或画线指标 | 数值序列 | 可以计算和导出；配置明确选股表达式后才能交易 |
| 八卦指标 | 卦象和动爻分类 | 运行条件收益统计，不直接产生交易信号 |

多个信号指标默认分别回测，不隐式合并。只有显式设置 `combine=all` 或 `combine=any` 时才执行 AND/OR。八卦不参加布尔组合。

### 5.3 八卦接口

```python
BaguaCalculator.calculate(
    *,
    open_price,
    high_price,
    low_price,
    close_price,
) -> BaguaResult
```

`BaguaResult` 至少返回上下卦编号、规范名、常用名和符号，卦序、卦符、卦名，动爻序号和爻名，以及卦辞、核心总纲、爻辞、行情简判、可选变卦和备注。

## 6. 分阶段实施清单

### 阶段 A：环境和基线

- 初始化 CodeGraph 索引。
- 使用 Python 3.9 x64 建立项目虚拟环境，保持与 `pandas==1.3.5` 兼容。
- 增加 `baostock`、`openpyxl` 和测试依赖，不全局升级项目依赖。
- 跑通现有 WonderTrader 最小股票回测，记录基线输出。

### 阶段 B：通达信日线数据链路

- 实现 32 字节 `.day` 记录解析，价格以整数分读取后精确转换。
- 过滤指数、基金、债券、B 股和北交所，只生成沪深 A 股合约。
- 转换为 DSB，建议路径为 `storage/astock/his/day/{SSE,SZSE}/STK{code}.dsb`。
- 从上证指数实际行情日期生成交易日历，不依赖已过期节假日文件。
- 使用 Baostock 生成复权因子；原始行情保留给八卦，动态复权行情供指标和收益使用。
- 生成 manifest，记录源文件、记录数、首尾日期、异常数和 SHA256。

### 阶段 C：指标注册与 TN6 混合导入

- 扫描 `指标` 目录，将现有 4 个 `.tn6` 全部注册为系统默认可见指标。
- 对 SHA256 相同的三个“双增”包只保存一个内容版本，同时保留三个名称别名。
- 注册三个现有 `.txt` 公式；未经通达信输出验证，不自动宣称其与某个 `.tn6` 等价。
- 实现 `.tn6` 与源码显式映射 manifest。
- `735金叉及趋势.tn6` 在源码补齐前保持 `source_required`。
- 提供指标列表、状态、来源、版本、支持周期和失败原因查询。

### 阶段 D：通达信公式语言

- 实现 tokenizer、parser、AST、编译器和向量化运行时，禁止使用 Python `eval`。
- 第一批支持赋值 `:=`、输出 `XG:`、算术、比较、布尔运算和括号。
- 第一批内置函数至少包含 `MA`、`EMA`、`REF`、`CROSS`、`BARSLAST`、`COUNT`。
- 明确实现空值、首段预热、交叉判断和布尔序列语义。
- 未支持函数必须返回源码位置和函数名，不得静默忽略或替代。
- `#MIN60` 引用进入依赖检查；分钟数据不足时禁止正式运行。

### 阶段 E：日周月和共振

- 日线直接使用原始交易日记录。
- 周/月分别按首个开盘、最高、最低、最后收盘和成交量求和聚合。
- 默认排除尚未闭合的当前周/月。
- V5 分别生成 D/W/M 信号。
- DWM 共振以日线收盘作为决策点，对齐最近已经闭合的周/月状态，严格禁止未来数据。

### 阶段 F：八卦知识库与计算

- 将 Excel 规范化为版本化 `bagua_384.json`，Excel 继续作为权威来源。
- 保存卦序、卦符、卦名、上下卦、爻序、爻名、卦辞、总纲、爻辞、行情简判、变卦、备注和源文件 SHA256。
- 校验 64 卦乘 6 爻完整性和必填字段。
- 使用 `Decimal` 或整数分规避浮点和末尾零错误。
- 注册 `bagua_ohlc` 原生指标，使其可以通过与通达信指标相同的选择接口启用。

### 阶段 G：研究、组合和报告

- 按指标、周期、卦象、动爻和是否共振统计未来 1/3/5 周期收益、胜率、中位数、样本数、MFE 和 MAE。
- 使用 WonderTrader `ET_SEL` 运行组合回测。
- 输出收益率、年化收益、最大回撤、波动率、Sharpe、胜率、换手和成本影响。
- 输出 CSV 明细和 Excel 汇总，保存运行配置、指标版本、数据版本和风险声明。

## 7. CLI

```text
python -m wtpy.apps.astock list-indicators
python -m wtpy.apps.astock import-indicator <file.tn6> --source <formula.txt>
python -m wtpy.apps.astock validate-indicator <indicator_id>
python -m wtpy.apps.astock inspect-data
python -m wtpy.apps.astock import-data
python -m wtpy.apps.astock build-signals --indicator <id> --period DAY
python -m wtpy.apps.astock backtest --indicator <id> --period DAY --hold 1
python -m wtpy.apps.astock backtest --indicator <id1> --indicator <id2> --combine all
python -m wtpy.apps.astock bagua-study --period WEEK
python -m wtpy.apps.astock report <run_id>
```

所有默认指标仅表示“默认出现在指标库”，不会自动全部启用；包括 `bagua_ohlc` 在内都必须显式选择。

## 8. 测试与验收

- 精确核对 `sh600000.day` 和 `sz000001.day` 的首尾日期及 OHLCV。
- 覆盖文件长度错误、日期乱序、重复数据、非正价格、OHLC 关系错误和异常成交量。
- 覆盖节假日短周、停牌、新上市、缺失交易日和未闭合周/月。
- 证明任一历史决策点都没有读取未来周线、月线或未来复权信息。
- 4 个 `.tn6` 均可导入展示；三个重复包只产生一个运行内容版本。
- 缺少源码的 `.tn6` 必须为 `source_required`，不能误标为可回测。
- 对 10 至 20 只黄金样本，将公式中间变量和最终 `XG` 与相同复权设置下的通达信结果逐日对比。
- 未知函数、错误参数、跨周期数据不足和语法错误必须给出确定性诊断。
- 八卦知识库必须严格为 64 卦乘 6 爻。
- 固定样例 `O=6.27,H=7.33,L=5.90,C=5.90` 必须得到 `䷃山水蒙`、`六三` 和“诱多陷阱不可追涨，观望为上”。
- 八卦单独选择时不得产生虚构交易信号；与 V5 同选时应正确附加到 V5 信号。
- 先完成小股票池和短区间集成测试，再运行 2016–2026 沪深全市场。
- 同一数据、配置和指标版本必须可重复生成一致结果。

## 9. 风险与第二阶段

- 当前本地通达信股票池可能缺少历史退市股票，第一阶段报告必须明确标记幸存者偏差。
- 历史 ST 状态、上市退市日期、板块规则和涨跌停规则不完整，会影响成交模拟。
- 分红送转需要复权和公司行为数据，否则技术指标与收益可能失真。
- `735金叉及趋势.tn6` 缺少公式源码；该源码是其从“可导入”变为“可回测”的必要输入。
- 分钟历史不足，所有依赖 `MIN60` 的正式长周期结果暂时为 No-Go。
- 第二阶段补齐历史证券主数据、ST 变化、精确涨跌停规则、分钟行情和退市股票后，才能把全市场结果视为正式结论。

## 10. Grok 交付要求

Grok 实施时不得修改本计划的业务语义，不得破解 `.tn6`，不得伪造缺失公式。完成后必须提供：

- 实际新增和修改文件清单。
- 关键架构和接口说明。
- 所有运行过的命令、测试结果和失败项。
- 小股票池回测证据及输出路径。
- 尚未完成的功能、外部数据缺口和风险。
- 一份根目录 `GROK_IMPLEMENTATION_REPORT.md`，供 Codex 后续独立审核。
- 未经用户明确授权，不得提交、推送或改写 Git 历史。
