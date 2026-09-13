# -*- coding: utf-8 -*-
"""导出 Excel 表结构读取助手（各导出测试共用）。

规则/信号 sheet 的表头上方有规则说明区（规则名称 / ID / 来源 / 基准日 /
命中只数 / 规则说明 / 占位原因），表格起点不再固定为第 1 行；
stock-all / index-all / etf-all 与 meta 仍从第 1 行开始。
"""
from __future__ import annotations


def table_start(ws) -> int:
    """数据表头所在行：向下找到第一个 A 列为 "code" 的行（默认 1）。"""
    for r in range(1, min(ws.max_row or 1, 12) + 1):
        if ws.cell(r, 1).value == "code":
            return r
    return 1


def header_values(ws) -> list:
    """数据表头单元格值（跨过规则说明区）。"""
    return [c.value for c in ws[table_start(ws)]]


def data_rows(ws) -> list:
    """数据行（跳过规则说明区与表头）。"""
    return list(ws.iter_rows(min_row=table_start(ws) + 1, values_only=True))


def brief_keys(ws) -> list:
    """规则说明区的 key 列表（说明区每行 A 列即 key）；无说明区时为空。"""
    return [ws.cell(r, 1).value for r in range(1, table_start(ws))]
