# Phase1 735 标准 16 组核对说明（P1.8）

固定技术日程（全部 16 组共用）：

- 信号：周五收盘确认 735（`signal_weekdays=[5]`，信号日 = 周五）
- 买入：下周一开盘（`buy_weekday=1`, `buy_on=open`）
- 风险：不设止损、不设止盈
- 账户/其它：以当次回测请求为准；成本见 CostConfig（run_meta / Excel 可追溯）

## 16 组矩阵轴

| 轴 | 取值 | 数量 |
|----|------|------|
| 卖出星期 exit_weekday | 2 周二 / 3 周三 / 4 周四 / 5 周五 | 4 |
| 卖出时点 sell_on | open / close | 2 |
| 卦象 | 无卦象（gua 关闭）/ 最佳三爻（best3 或等价 gua_filter） | 2 |

组合数：4 × 2 × 2 = 16。

## 编号约定（核对表行号）

| 组号 | exit_weekday | sell_on | 卦象 | 说明 |
|------|--------------|---------|------|------|
| G01 | 2 | open | 无 | 周二开盘平 |
| G02 | 2 | close | 无 | 周二收盘平 |
| G03 | 3 | open | 无 | 周三开盘平 |
| G04 | 3 | close | 无 | 周三收盘平 |
| G05 | 4 | open | 无 | 周四开盘平 |
| G06 | 4 | close | 无 | 周四收盘平 |
| G07 | 5 | open | 无 | 周五开盘平 |
| G08 | 5 | close | 无 | 周五收盘平 |
| G09 | 2 | open | 最佳三爻 | 同上 + best3 |
| G10 | 2 | close | 最佳三爻 | |
| G11 | 3 | open | 最佳三爻 | |
| G12 | 3 | close | 最佳三爻 | |
| G13 | 4 | open | 最佳三爻 | |
| G14 | 4 | close | 最佳三爻 | |
| G15 | 5 | open | 最佳三爻 | |
| G16 | 5 | close | 最佳三爻 | |

## 自动化测试覆盖（Phase1）

| 测试文件 | 覆盖组/行为 | 备注 |
|----------|-------------|------|
| `tests/apps/astock/test_735_phase1_schedule.py` | G01/G03/G05/G07 日程子集（无卦，open 平仓）+ G04 型 close 一例 | 合成 K 线；断言 Mon 买 + 对应平仓日 + `weekday_exit` |
| `test_735_phase1_schedule.py::test_735_schedule_mon_buy_exit_weekdays_open` | exit_wd ∈ {2,3,4,5} × sell_on=open × 无卦 | 对应 G01/G03/G05/G07 |
| `test_735_phase1_schedule.py::test_735_schedule_exit_close_session` | exit_wd=3 × sell_on=close × 无卦 | 对应 G04 |
| `tests/apps/astock/test_735_phase1_gua_contrast.py` | G03 日程 + 卦象开关对照 | mock `gua_filter` 丢弃信号 → 成交笔数不同 |
| `test_735_phase1_gua_contrast.py::test_gua_filter_only_in_service_layer_not_portfolio_backtester` | 架构说明 | **卦象过滤仅在 BacktestService / filter_rules 路径**，不在 `PortfolioBacktester.run` |
| `test_735_phase1_gua_contrast.py::test_735_schedule_gua_disabled_vs_mock_drop_trade_counts_differ` | 无卦 vs 过滤后 0 信号 | 服务层过滤后再交给引擎 |
| `test_735_phase1_gua_contrast.py::test_result_config_includes_full_cost_fields` | 成本字段 | commission_rate / min_commission / stamp_tax_rate / slippage / note |
| `test_735_phase1_gua_contrast.py::test_write_backtest_csv_run_meta_and_excel_costs` | 产物追溯 | `run_meta.json` + Excel 汇总含完整 CostConfig |
| `tests/apps/astock/test_gua_filter.py` | 过滤规则单元 | best3 / exact_line 等（支撑 G09–G16 过滤语义） |
| `tests/apps/astock/test_weekday_schedule.py` 等 | 通用星期日程 | 非 735 专名，共享 buy/exit_weekday 语义 |

## 未自动化（留给 Phase2 矩阵 / 手工）

- G02、G06、G08 的 close 全组合（Phase1 仅 G04 close 样例）
- G09–G16 真实行情 + 真实 bagua attach 的全量 8 组（Phase1 用 mock 丢弃对照证明过滤生效）
- 全市场 16 组收益矩阵与 Excel 总表（P2.7）

## 成本口径（P1.7）追溯位置

- `CostConfig`：`wtpy/apps/astock/config.py`
- 引擎结果：`BacktestResult.config["costs"]`（`strategy.py`）
- 服务摘要：`BacktestService` 返回的 `summary["costs"]` 与 `repro["costs"]`
- 落盘：`run_meta.json` 顶层 `costs`（及 `config.costs`）
- Excel：`summary.xlsx` 汇总页「【成本口径 CostConfig】」五行

## 运行命令

```text
python -m pytest tests/apps/astock/test_735_phase1_schedule.py tests/apps/astock/test_735_phase1_gua_contrast.py -q
```
