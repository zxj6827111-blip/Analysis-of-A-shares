# -*- coding: utf-8 -*-
"""CSV column contracts for price-lane exports (keeps reports.py thinner).

Three planes: L1 signal (asof_forward_qfq / ordinary qfq audit refs),
L2 trade (price/raw_price), L3 CA ledger fields on CA-aware exit fills.
"""

from __future__ import annotations

from typing import List

# Bumped when a report column meaning changes in a way consumers must
# detect (written to run_meta.json as report_schema_version).
# v2: CA-aware 毛利润/净利润 (cash dividend added, cost-basis denominator)
#     plus explicit CA现金分红 / 持仓成本基数 columns.
REPORT_SCHEMA_VERSION: int = 2

FILL_CSV_FIELDS: List[str] = [
    "date",
    "std_code",
    "side",
    "price",
    "raw_price",
    "execution_price",
    "adjusted_reference_price",
    "point_in_time_reference_price",
    "standard_qfq_reference_price",
    "adjustment_factor",
    "adjustment_scale",
    "qfq_scale",
    "point_scale",
    "price_session",
    "price_source",
    "shares",
    "amount",
    "position_cost_basis",
    "corporate_action_cash_received",
    "commission",
    "stamp_tax",
    "reason",
]

TRADE_TRIP_FIELDS: List[str] = [
    "序号",
    "证券代码",
    "代码",
    "信号日期",
    "指标",
    "卦名",
    "爻位",
    "卦序",
    "变卦",
    "操作信号",
    "state_id",
    "卦象简判",
    "买入日期",
    "买入价",
    "买入价_起点锚定研究参考",
    "买入价_普通前复权参考",
    "买入复权因子",
    "买入复权比例",
    "卖出日期",
    "卖出价",
    "卖出价_起点锚定研究参考",
    "卖出价_普通前复权参考",
    "卖出复权因子",
    "卖出复权比例",
    "数量",
    "买入金额",
    "卖出金额",
    "买入手续费",
    "卖出手续费及印花税",
    "CA现金分红",
    "持仓成本基数",
    "毛利润",
    "净利润",
    "毛收益率",
    "净收益率",
    "是否盈利",
    "卖出原因",
    "状态",
]
