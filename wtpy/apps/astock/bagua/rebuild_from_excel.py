# -*- coding: utf-8 -*-
"""Rebuild bagua_384.json from the authoritative Excel（64卦384爻行情简判）.

数据源演进（两版布局，列数不同）
--------------------------------
- 9 列版（``…-已添加操作信号.xlsx``）：卦象卦名/卦辞原文/卦核心总纲/爻位/变卦/
  爻辞原文/个股行情简判/备注&实操总结/操作信号。其中 210 爻「变卦」留空，
  由本模块按动爻翻转推算补齐。
- 10 列版（``…-260911.xlsx``，当前权威稿）：新增「变卦全称」列，变卦 384/384
  填全。本模块当场交叉校验「Excel 值 vs 动爻推算」，实测 384/384 一致，
  故改为**以 Excel 为数据源、以推算为断言**：任何不一致都会写进
  ``biangua_fill`` 与校验报告，而不是静默采信。

因此本模块按**表头名**取列，不再按列序号硬编码——旧版脚本按范围 ``1..9`` 取值，
遇到 10 列布局会把「变卦全称」当爻辞、整体错位且不报错。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TRIGRAMS = {
    1: {"id": 1, "name": "乾", "alias": "天", "symbol": "☰"},
    2: {"id": 2, "name": "兑", "alias": "泽", "symbol": "☱"},
    3: {"id": 3, "name": "离", "alias": "火", "symbol": "☲"},
    4: {"id": 4, "name": "震", "alias": "雷", "symbol": "☳"},
    5: {"id": 5, "name": "巽", "alias": "风", "symbol": "☴"},
    6: {"id": 6, "name": "坎", "alias": "水", "symbol": "☵"},
    7: {"id": 7, "name": "艮", "alias": "山", "symbol": "☶"},
    8: {"id": 8, "name": "坤", "alias": "地", "symbol": "☷"},
}

YAO_ORDER_MAP = {
    "初九": 1,
    "初六": 1,
    "九二": 2,
    "六二": 2,
    "九三": 3,
    "六三": 3,
    "九四": 4,
    "六四": 4,
    "九五": 5,
    "六五": 5,
    "上九": 6,
    "上六": 6,
}

SELF_MAP = {
    "乾为天": (1, 1),
    "坤为地": (8, 8),
    "震为雷": (4, 4),
    "艮为山": (7, 7),
    "坎为水": (6, 6),
    "离为火": (3, 3),
    "巽为风": (5, 5),
    "兑为泽": (2, 2),
}

DEFAULT_RULE_VERSION = "gua_rules_v20260911"

# 人工标注的操作信号词表（20260911 稿新增 观察/不碰/持有或开仓 三个取值）。
# 仅用于校验报告里提示「出现词表外的信号」，不作为写入白名单——数据以 Excel 为准。
ACTION_SIGNAL_VOCAB: Tuple[str, ...] = (
    "新开仓",
    "加仓",
    "持有或开仓",
    "持有",
    "观察",
    "减仓",
    "不碰",
    "清仓",
)

# 表头名 → 内部字段。允许同义表头（旧版叫「备注」），未知列直接忽略。
COLUMN_FIELDS: Dict[str, str] = {
    "卦象卦名": "full_name",
    "卦辞原文": "gua_ci",
    "卦核心总纲": "core_gang",
    "爻位": "yao_name",
    "变卦": "biangua_short",
    "变卦全称": "biangua_full",
    "爻辞原文": "yao_ci",
    "个股行情简判": "judgement",
    "备注&实操总结": "note",
    "备注": "note",
    "操作信号": "action_signal",
}
REQUIRED_FIELDS = ("full_name", "yao_name", "yao_ci")

# 历史遗留的变卦纠错记录：旧版 Excel 有 2 处变卦写法有误，当时由本模块纠正并在
# JSON 里留痕。20260911 稿的「变卦全称」已与推算完全一致（乾九四改回小畜），
# 故以下记录只作历史说明，不再是待修项。
HISTORICAL_BIANGUA_CORRECTIONS: List[dict] = [
    {
        "state_id": "01-4",
        "full_name": "䷀乾为天",
        "yao_name": "九四",
        "excel_biangua": "中孚",
        "correct": "风天小畜",
        "status": "resolved_in_260911",
        "note": "旧版 Excel 写「中孚」，动爻推算为「风天小畜」；20260911 稿源码已改为小畜",
    },
    {
        "state_id": "21-6",
        "full_name": "䷔火雷噬嗑",
        "yao_name": "上九",
        "excel_biangua": "雷",
        "correct": "震为雷",
        "status": "resolved_in_260911",
        "note": "旧版 Excel 只写单字「雷」，20260911 稿「变卦全称」写全为「震为雷」",
    },
]

_ELEMENTS = "天地水火雷风山泽"


def _alias_to_id() -> Dict[str, int]:
    return {v["alias"]: k for k, v in TRIGRAMS.items()}


def _id_to_name() -> Dict[int, str]:
    return {k: v["name"] for k, v in TRIGRAMS.items()}


def _id_to_alias() -> Dict[int, str]:
    return {k: v["alias"] for k, v in TRIGRAMS.items()}


def split_full_name(full: object) -> Tuple[str, str]:
    """'䷀乾为天' → ('䷀', '乾为天')；无卦符时返回 ('', 原名)。

    卦符位于 U+4DC0 起的「易經六十四卦符號」区，不属于 CJK 统一汉字区，
    因此用「前导非汉字」切分即可，缺失卦符也不会把首字误当卦符。
    """
    s = str(full or "").strip()
    if not s:
        return "", ""
    m = re.match(r"^([^\u4e00-\u9fff]*)([\u4e00-\u9fff].*)$", s)
    if not m:
        return "", s
    return (m.group(1) or "").strip(), (m.group(2) or "").strip()


def parse_ul(name: str) -> Tuple[int, int]:
    if name in SELF_MAP:
        return SELF_MAP[name]
    alias_to_id = _alias_to_id()
    return alias_to_id[name[0]], alias_to_id[name[1]]


def make_state_id(gua_order: int, yao_order: int) -> str:
    return f"{int(gua_order):02d}-{int(yao_order)}"


def _cell_text(v: object) -> str:
    return "" if v is None else str(v).strip()


def read_column_map(ws) -> Dict[str, int]:
    """按表头名建立 字段 → 列号 映射（1-based），忽略未知列。

    旧脚本按列序号取值，列数一变就静默错位；这里改为按名取列，缺列直接报错。
    """
    col_map: Dict[str, int] = {}
    for c in range(1, ws.max_column + 1):
        header = _cell_text(ws.cell(1, c).value)
        field = COLUMN_FIELDS.get(header)
        if field and field not in col_map:
            col_map[field] = c
    missing = [f for f in REQUIRED_FIELDS if f not in col_map]
    if missing:
        raise ValueError(
            "Excel 表头缺少必需列 %s（实际表头：%s）"
            % (missing, [_cell_text(ws.cell(1, c).value) for c in range(1, ws.max_column + 1)])
        )
    return col_map


def _row_values(ws, r: int, col_map: Dict[str, int]) -> Dict[str, str]:
    return {f: _cell_text(ws.cell(r, c).value) for f, c in col_map.items()}


def rebuild_knowledge_from_excel(
    xlsx_path: Path | str,
    out_json: Path | str,
    *,
    rule_version: str = DEFAULT_RULE_VERSION,
) -> dict:
    """Rebuild bagua_384.json from Excel authority（自动识别 9/10 列布局）。"""
    import openpyxl

    xlsx_path = Path(xlsx_path)
    out_json = Path(out_json)
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    sha = hashlib.sha256(xlsx_path.read_bytes()).hexdigest()

    headers = [_cell_text(ws.cell(1, c).value) for c in range(1, ws.max_column + 1)]
    col_map = read_column_map(ws)

    entries: List[dict] = []
    current: Optional[dict] = None
    gua_idx = 0
    r = 2
    while True:
        vals = _row_values(ws, r, col_map)
        if all(not v for v in vals.values()):
            break
        gname = vals.get("full_name", "")
        gci = vals.get("gua_ci", "")
        gang = vals.get("core_gang", "")
        if gname:
            if current is None or gname != current["full_name"]:
                gua_idx += 1
                symbol, rest = split_full_name(gname)
                current = {
                    "gua_order": gua_idx,
                    "full_name": gname,
                    "gua_symbol": symbol,
                    "gua_name": rest,
                    "gua_ci": gci,
                    "core_gang": gang,
                }
            else:
                if gci:
                    current["gua_ci"] = gci
                if gang:
                    current["core_gang"] = gang
        elif current is None:
            raise ValueError(f"missing hexagram name at row {r}")
        else:
            # 同一卦的后续爻行留空属正常（合并单元格式写法），但若写了值则以该行为准
            if gci:
                current["gua_ci"] = gci
            if gang:
                current["core_gang"] = gang

        yao = vals.get("yao_name", "")
        yao_order = YAO_ORDER_MAP.get(yao)
        if yao_order is None:
            raise ValueError(f"unknown yao at row {r}: {yao!r}")
        u, lo = parse_ul(current["gua_name"])
        state_id = make_state_id(current["gua_order"], yao_order)
        entries.append(
            {
                "excel_row": r,
                "state_id": state_id,
                "main_hexagram_id": current["gua_order"],
                "gua_order": current["gua_order"],
                "gua_symbol": current["gua_symbol"],
                "hexagram_symbol": current["gua_symbol"],
                "gua_name": current["gua_name"],
                "main_hexagram_name": current["gua_name"],
                "full_name": current["full_name"],
                "gua_ci": current["gua_ci"],
                "core_gang": current["core_gang"],
                "yao_order": yao_order,
                "line_index": yao_order,
                "yao_name": yao,
                "line_name": yao,
                "biangua": "",
                "changed_hexagram_name": "",
                "changed_hexagram_id": None,
                "biangua_full_name": "",
                "yao_ci": vals.get("yao_ci", ""),
                "line_text": vals.get("yao_ci", ""),
                "market_judgement": vals.get("judgement", ""),
                "market_summary": vals.get("judgement", ""),
                "note": vals.get("note", ""),
                "action_signal": vals.get("action_signal", ""),
                "upper": u,
                "lower": lo,
                "upper_name": TRIGRAMS[u]["name"],
                "lower_name": TRIGRAMS[lo]["name"],
                "upper_alias": TRIGRAMS[u]["alias"],
                "lower_alias": TRIGRAMS[lo]["alias"],
                "upper_symbol": TRIGRAMS[u]["symbol"],
                "lower_symbol": TRIGRAMS[lo]["symbol"],
                # 解析期的临时字段，resolve_biangua 用完后清除
                "_excel_biangua_short": vals.get("biangua_short", ""),
                "_excel_biangua_full": vals.get("biangua_full", ""),
            }
        )
        r += 1

    # 卦辞/总纲只在每卦首行出现，按卦补齐到六爻
    by_go: Dict[int, List[dict]] = {}
    for e in entries:
        by_go.setdefault(e["gua_order"], []).append(e)
    for go, rows in by_go.items():
        gang = next((x["core_gang"] for x in rows if x["core_gang"]), "")
        gci = next((x["gua_ci"] for x in rows if x["gua_ci"]), "")
        for x in rows:
            x["core_gang"] = gang
            x["gua_ci"] = gci

    kb = {
        "source_file": xlsx_path.name,
        "source_sha256": sha,
        "source_path": str(xlsx_path.resolve()),
        "rule_version": rule_version,
        "trigrams": TRIGRAMS,
        "entries": entries,
        "count_gua": len(by_go),
        "count_yao": len(entries),
        "headers": headers,
    }
    resolve_biangua(kb)
    sigs = Counter(e["action_signal"] for e in entries if e["action_signal"])
    kb["action_signal_counts"] = dict(sigs)
    kb["empty_biangua_count"] = sum(1 for e in entries if not e["biangua"])
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(kb, ensure_ascii=False, indent=2), encoding="utf-8")
    return kb


# Trigram lines bottom→top: 1=yang —, 0=yin - -
# 乾YYY 兑YYN 离YNY 震YNN 巽NYY 坎NYN 艮NNY 坤NNN
_TRIGRAM_LINES: Dict[int, Tuple[int, int, int]] = {
    1: (1, 1, 1),  # 乾
    2: (1, 1, 0),  # 兑
    3: (1, 0, 1),  # 离
    4: (1, 0, 0),  # 震
    5: (0, 1, 1),  # 巽
    6: (0, 1, 0),  # 坎
    7: (0, 0, 1),  # 艮
    8: (0, 0, 0),  # 坤
}
_LINES_TO_TRIGRAM: Dict[Tuple[int, int, int], int] = {v: k for k, v in _TRIGRAM_LINES.items()}


def short_gua_name(gua_name: str) -> str:
    """Excel-style short biangua label: 乾为天→乾, 天火同人→同人, 风天小畜→小畜."""
    name = str(gua_name or "").strip()
    if not name:
        return ""
    if "为" in name:
        return name.split("为", 1)[0]
    elements = set(_ELEMENTS)
    if len(name) >= 3 and name[0] in elements and name[1] in elements:
        return name[2:]
    return name


def changed_hexagram_ul(upper: int, lower: int, yao_order: int) -> Tuple[int, int]:
    """Flip the moving line (1=初 … 6=上) → (new_upper, new_lower)."""
    yo = int(yao_order)
    if yo < 1 or yo > 6:
        raise ValueError(f"yao_order out of range: {yao_order}")
    lo = list(_TRIGRAM_LINES[int(lower)])
    up = list(_TRIGRAM_LINES[int(upper)])
    lines = lo + up  # index 0 = 初爻
    i = yo - 1
    lines[i] = 1 - lines[i]
    new_lo = _LINES_TO_TRIGRAM[tuple(lines[0:3])]
    new_up = _LINES_TO_TRIGRAM[tuple(lines[3:6])]
    return int(new_up), int(new_lo)


def _resolve_short_to_order(short: str, name_to_order: Dict[str, int]) -> Optional[int]:
    """把 Excel「变卦」列的松散写法解析成卦序（1..64），解析不了返回 None。

    实测写法共有四类：全称（风天小畜）、去卦符全称、截短（小畜/同人）、
    本卦单字（乾/坤/震/艮/坎/离/巽/兑）及「坎水/坤地/震雷/离火」这类
    三合写法。逐级尝试，命中即返回；歧义不猜，交给推算值。
    """
    s = str(short or "").strip()
    if not s:
        return None
    if s in name_to_order:
        return name_to_order[s]
    # 本卦单字：乾/坤/震/艮/坎/离/巽/兑 → 本卦全称（乾为天 / 坎为水 …）
    id_to_name = _id_to_name()
    id_to_alias = _id_to_alias()
    for tid, tname in id_to_name.items():
        if s == tname:
            return name_to_order.get(f"{tname}为{id_to_alias[tid]}")
    # 三合写法：坎水/坤地/震雷/离火/乾天/兑泽/艮山/巽风
    for tid, tname in id_to_name.items():
        if s == tname + id_to_alias[tid]:
            return name_to_order.get(f"{tname}为{id_to_alias[tid]}")
    # 去元素前缀：水火既济 → 既济
    if len(s) >= 3 and s[0] in _ELEMENTS and s[1] in _ELEMENTS:
        return name_to_order.get(s[2:])
    return None


def build_name_index(entries: List[dict]) -> Tuple[Dict[str, int], Dict[int, dict]]:
    """建立 卦名/全称/截短名 → 卦序 与 卦序 → 卦元数据 两个索引（以本文件为准）。"""
    name_to_order: Dict[str, int] = {}
    order_to_meta: Dict[int, dict] = {}
    for e in entries:
        go = int(e["gua_order"])
        meta = order_to_meta.setdefault(
            go,
            {
                "gua_order": go,
                "gua_name": e.get("gua_name") or "",
                "full_name": e.get("full_name") or "",
            },
        )
        for key in (e.get("gua_name") or "", e.get("full_name") or ""):
            key = str(key).strip()
            if not key:
                continue
            symbol, plain = split_full_name(key)
            for variant in (key, plain, short_gua_name(plain)):
                if variant:
                    name_to_order.setdefault(variant, go)
    return name_to_order, order_to_meta


def resolve_biangua(kb: dict) -> Dict[str, Any]:
    """以 Excel 为准写入变卦，并用动爻推算法交叉校验。

    三条规则：
    1. Excel「变卦全称」能解析 → 采信 Excel，与推算不一致记 conflict（不静默）；
    2. 只有松散「变卦」列 → 解析后采信，解析不出则用推算；
    3. 两列都空（旧版 210 爻）→ 用推算补齐，标 computed。

    最终 ``biangua`` 统一存**截短名**（乾/坤/小畜/同人…），与筛选、展示口径一致；
    ``biangua_full_name`` 存 Excel 全称（如「风天小畜」）。
    """
    entries: List[dict] = list(kb.get("entries") or [])
    name_to_order, order_to_meta = build_name_index(entries)
    ul_to_order: Dict[Tuple[int, int], int] = {}
    for e in entries:
        ul_to_order.setdefault((int(e["upper"]), int(e["lower"])), int(e["gua_order"]))

    stats = Counter()
    conflicts: List[dict] = []
    normalized: List[dict] = []

    for e in entries:
        excel_short = str(e.pop("_excel_biangua_short", "") or "").strip()
        excel_full = str(e.pop("_excel_biangua_full", "") or "").strip()

        new_u, new_l = changed_hexagram_ul(int(e["upper"]), int(e["lower"]), int(e["yao_order"]))
        computed_order = ul_to_order.get((new_u, new_l))
        computed_meta = order_to_meta.get(computed_order) or {}
        computed_full = str(computed_meta.get("gua_name") or "")

        excel_order = None
        excel_source = ""
        if excel_full:
            _, plain = split_full_name(excel_full)
            excel_order = name_to_order.get(plain) or _resolve_short_to_order(plain, name_to_order)
            excel_source = "full_name"
        if excel_order is None and excel_short:
            excel_order = _resolve_short_to_order(excel_short, name_to_order)
            excel_source = "short"

        if excel_order is None:
            # 无可用 Excel 值 → 推算补齐
            e["biangua"] = short_gua_name(computed_full)
            e["changed_hexagram_name"] = e["biangua"]
            e["changed_hexagram_id"] = computed_order
            e["biangua_full_name"] = computed_full
            e["biangua_source"] = "computed"
            stats["computed"] += 1
            continue

        meta = order_to_meta.get(excel_order) or {}
        e["biangua"] = short_gua_name(str(meta.get("gua_name") or ""))
        e["changed_hexagram_name"] = e["biangua"]
        e["changed_hexagram_id"] = excel_order
        e["biangua_full_name"] = str(meta.get("gua_name") or excel_full)
        if excel_order == computed_order:
            e["biangua_source"] = "excel"
            stats["excel"] += 1
        else:
            e["biangua_source"] = "excel_conflict"
            stats["excel_conflict"] += 1
            conflicts.append(
                {
                    "state_id": e["state_id"],
                    "full_name": e["full_name"],
                    "yao_name": e["yao_name"],
                    "excel_biangua": excel_full or excel_short,
                    "excel_resolved": meta.get("gua_name") or "",
                    "computed": computed_full,
                    "source": excel_source,
                }
            )
        # 同一卦的不同写法（坎水 vs 坎为水）需要归一，留痕便于回溯
        if excel_short and excel_short != e["biangua"]:
            normalized.append(
                {
                    "state_id": e["state_id"],
                    "excel_biangua": excel_short,
                    "canonical": e["biangua"],
                    "full_name": e["biangua_full_name"],
                }
            )

    empty_left = sum(1 for e in entries if not str(e.get("biangua") or "").strip())
    missing_full = sum(1 for e in entries if not str(e.get("biangua_full_name") or "").strip())
    kb["entries"] = entries
    kb["empty_biangua_count"] = empty_left
    kb["biangua_fill"] = {
        "filled_computed": stats["computed"],
        "kept_excel": stats["excel"],
        "excel_conflict": stats["excel_conflict"],
        "empty_left": empty_left,
        "missing_full_name": missing_full,
        "method": "excel_first_with_line_flip_check",
        "excel_normalized": normalized,
    }
    kb["biangua_conflicts"] = conflicts
    kb["biangua_corrections"] = HISTORICAL_BIANGUA_CORRECTIONS
    return kb


def fill_missing_biangua(kb: dict, *, only_empty: bool = True) -> Dict[str, Any]:
    """兼容旧接口：仅当变卦为空时用动爻翻转补齐（保留已有 Excel 值）。

    新流程走 :func:`resolve_biangua`；此函数保留给外部脚本/测试调用。
    """
    entries: List[dict] = list(kb.get("entries") or [])
    _, order_to_meta = build_name_index(entries)
    ul_to_order: Dict[Tuple[int, int], int] = {}
    for e in entries:
        ul_to_order.setdefault((int(e["upper"]), int(e["lower"])), int(e["gua_order"]))

    filled = kept = failed = 0
    for e in entries:
        existing = str(e.get("biangua") or e.get("changed_hexagram_name") or "").strip()
        if only_empty and existing:
            e["biangua_source"] = e.get("biangua_source") or "excel"
            kept += 1
            continue
        try:
            nu, nl = changed_hexagram_ul(int(e["upper"]), int(e["lower"]), int(e["yao_order"]))
        except Exception:
            failed += 1
            continue
        meta = order_to_meta.get(ul_to_order.get((nu, nl)))
        if not meta:
            failed += 1
            continue
        short = short_gua_name(str(meta["gua_name"]))
        e["biangua"] = short
        e["changed_hexagram_name"] = short
        e["changed_hexagram_id"] = meta.get("gua_order")
        e["biangua_source"] = "computed"
        e["biangua_full_name"] = meta.get("gua_name") or short
        filled += 1
    empty_left = sum(1 for e in entries if not str(e.get("biangua") or "").strip())
    kb["entries"] = entries
    kb["empty_biangua_count"] = empty_left
    kb["biangua_fill"] = {
        "filled_computed": filled,
        "kept_excel": kept,
        "failed": failed,
        "empty_left": empty_left,
        "method": "line_flip_bottom_to_top",
    }
    return kb


def validate_knowledge(kb: dict) -> Dict[str, Any]:
    entries = kb.get("entries") or []
    issues: List[str] = []
    warnings: List[str] = []
    by_go: Dict[int, List[dict]] = defaultdict(list)
    for e in entries:
        by_go[int(e["gua_order"])].append(e)
    if len(entries) != 384:
        issues.append(f"expected 384 states, got {len(entries)}")
    if len(by_go) != 64:
        issues.append(f"expected 64 main hexagrams, got {len(by_go)}")
    sids = [e.get("state_id") for e in entries]
    if len(sids) != len(set(sids)):
        issues.append("duplicate state_id")
    for go, rows in sorted(by_go.items()):
        if len(rows) != 6:
            issues.append(f"gua_order={go} has {len(rows)} lines, expected 6")
        orders = sorted(int(r["yao_order"]) for r in rows)
        if orders != [1, 2, 3, 4, 5, 6]:
            issues.append(f"gua_order={go} bad yao_order {orders}")
    # 变卦：Excel 与推算必须一致（不一致时 resolve_biangua 会记 conflict）
    bad_bg = [e["state_id"] for e in entries if e.get("biangua_source") == "excel_conflict"]
    if bad_bg:
        issues.append(f"biangua excel/computed conflict: {bad_bg[:5]} (+{max(0, len(bad_bg) - 5)})")
    missing_sig = sum(1 for e in entries if not e.get("action_signal"))
    undocumented = sorted({str(e.get("action_signal") or "") for e in entries} - set(ACTION_SIGNAL_VOCAB) - {""})
    if undocumented:
        warnings.append(f"action_signal outside vocab: {undocumented}")
    empty_bg = sum(1 for e in entries if not e.get("biangua"))
    missing_full = sum(1 for e in entries if not e.get("biangua_full_name"))
    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "count_entries": len(entries),
        "count_gua": len(by_go),
        "unique_state_ids": len(set(sids)),
        "action_signal_counts": dict(Counter(e.get("action_signal") or "" for e in entries)),
        "empty_biangua": empty_bg,
        "missing_biangua_full_name": missing_full,
        "missing_action_signal": missing_sig,
        "notes_filled": sum(1 for e in entries if e.get("note")),
        "biangua_fill": kb.get("biangua_fill"),
        "rule_version": kb.get("rule_version"),
        "source_file": kb.get("source_file"),
        "source_sha256": kb.get("source_sha256"),
    }


def default_excel_path(repo_root: Optional[Path] = None) -> Path:
    """定位权威 Excel：优先最新稿（260911），再退回历史版本。"""
    root = repo_root or Path(__file__).resolve().parents[4]
    # parents: bagua -> astock -> apps -> wtpy -> repo
    ind = root / "指标"
    preferred = [
        ind / "64卦384爻行情简判完整版-260911.xlsx",
        ind / "64卦384爻行情简判完整版-已添加操作信号.xlsx",
        ind / "64卦384爻行情简判完整版-已添加操作信号(1).xlsx",
    ]
    for c in preferred:
        if c.exists():
            return c
    if ind.is_dir():
        for p in ind.glob("*操作信号*.xlsx"):
            return p
        for p in ind.glob("*384*.xlsx"):
            return p
    raise FileNotFoundError("no bagua excel found under 指标/")


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv or sys.argv[1:])
    bagua_dir = Path(__file__).resolve().parent
    out_json = bagua_dir / "bagua_384.json"
    report_path = bagua_dir / "gua_validation_report.json"
    xlsx = Path(argv[0]) if argv else default_excel_path(Path(__file__).resolve().parents[4])
    if not xlsx.exists():
        raise FileNotFoundError(f"excel not found: {xlsx}")
    print("excel:", xlsx)
    kb = rebuild_knowledge_from_excel(xlsx, out_json)
    report = validate_knowledge(kb)
    report["out_json"] = str(out_json)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
