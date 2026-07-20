"""Bagua OHLC calculator and knowledge base."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
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


@dataclass
class BaguaResult:
    open_price: str
    high_price: str
    low_price: str
    close_price: str
    open_digit_sum: int
    high_digit_sum: int
    low_digit_sum: int
    close_digit_sum: int
    upper_id: int
    lower_id: int
    upper_name: str
    lower_name: str
    upper_alias: str
    lower_alias: str
    upper_symbol: str
    lower_symbol: str
    yao_order: int
    yao_name: str
    gua_order: Optional[int]
    gua_symbol: str
    gua_name: str
    full_name: str
    gua_ci: str
    core_gang: str
    yao_ci: str
    market_judgement: str
    biangua: str
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


def format_price_2(price) -> str:
    d = Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{d:.2f}"


def digit_sum_price(price) -> int:
    s = format_price_2(price)
    total = 0
    for ch in s:
        if ch.isdigit():
            total += int(ch)
    return total


def mod_map(value: int, modulus: int) -> int:
    r = value % modulus
    return modulus if r == 0 else r


class BaguaKnowledge:
    REQUIRED_FIELDS = (
        "gua_order",
        "gua_symbol",
        "gua_name",
        "full_name",
        "gua_ci",
        "core_gang",
        "yao_order",
        "yao_name",
        "yao_ci",
        "market_judgement",
        "upper",
        "lower",
    )

    def __init__(self, path: Path | str):
        self.path = Path(path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.source_file = raw.get("source_file")
        self.source_sha256 = raw.get("source_sha256")
        self.source_path = raw.get("source_path")
        self.trigrams = raw.get("trigrams") or TRIGRAMS
        self.entries: List[dict] = raw.get("entries") or []
        self._by_key: Dict[Tuple[int, int, int], dict] = {}
        self._by_gua_order: Dict[int, List[dict]] = {}
        self._by_ul: Dict[Tuple[int, int], List[dict]] = {}
        for e in self.entries:
            key = (int(e["upper"]), int(e["lower"]), int(e["yao_order"]))
            self._by_key[key] = e
            go = int(e["gua_order"])
            self._by_gua_order.setdefault(go, []).append(e)
            ul = (int(e["upper"]), int(e["lower"]))
            self._by_ul.setdefault(ul, []).append(e)

    def validate(self) -> List[str]:
        issues: List[str] = []
        if len(self.entries) != 384:
            issues.append(f"expected 384 yaos, got {len(self.entries)}")
        if len(self._by_gua_order) != 64:
            issues.append(f"expected 64 gua_order, got {len(self._by_gua_order)}")
        expected_orders = set(range(1, 65))
        got_orders = set(self._by_gua_order.keys())
        if got_orders != expected_orders:
            issues.append(
                f"gua_order set mismatch missing={sorted(expected_orders-got_orders)[:5]} "
                f"extra={sorted(got_orders-expected_orders)[:5]}"
            )
        for go, rows in self._by_gua_order.items():
            if len(rows) != 6:
                issues.append(f"gua_order={go} has {len(rows)} yaos, expected 6")
                continue
            orders = sorted(int(r["yao_order"]) for r in rows)
            if orders != [1, 2, 3, 4, 5, 6]:
                issues.append(f"gua_order={go} bad yao_order {orders}")
            # group-level field consistency
            for fld in ("full_name", "gua_name", "gua_symbol", "gua_ci", "core_gang", "upper", "lower"):
                vals = {str(r.get(fld) or "") for r in rows}
                if len(vals) != 1:
                    issues.append(f"gua_order={go} inconsistent {fld}: {vals}")
            if not rows[0].get("core_gang"):
                issues.append(f"gua_order={go} empty core_gang")
            if not rows[0].get("gua_ci"):
                issues.append(f"gua_order={go} empty gua_ci")
        for i, e in enumerate(self.entries):
            for f in self.REQUIRED_FIELDS:
                if e.get(f) in (None, ""):
                    issues.append(f"entry[{i}] missing {f} ({e.get('full_name')})")
                    break
        return issues

    def lookup(self, upper: int, lower: int, yao_order: int) -> Optional[dict]:
        return self._by_key.get((upper, lower, yao_order))

    def excel_consistency_check(self, xlsx_path: Path | str) -> List[str]:
        """Row-by-row compare against Excel authority (A1:H385)."""
        import openpyxl

        xlsx_path = Path(xlsx_path)
        issues: List[str] = []
        if not xlsx_path.exists():
            return [f"excel missing: {xlsx_path}"]
        digest = hashlib.sha256(xlsx_path.read_bytes()).hexdigest()
        if self.source_sha256 and digest != self.source_sha256:
            issues.append(
                f"source sha mismatch json={self.source_sha256} excel={digest}"
            )
        wb = openpyxl.load_workbook(xlsx_path, data_only=True)
        ws = wb.active
        # rebuild expected names/yao/judgement with same forward-fill as rebuild
        # Compare each entry excel_row if present, else sequential
        for idx, e in enumerate(self.entries):
            r = int(e.get("excel_row") or (idx + 2))
            a = ws.cell(r, 1).value
            d = ws.cell(r, 4).value
            f = ws.cell(r, 6).value
            g = ws.cell(r, 7).value
            if a is not None and str(a).strip() != e.get("full_name"):
                issues.append(f"row {r} full_name excel={a!r} json={e.get('full_name')!r}")
            if d is not None and str(d).strip() != e.get("yao_name"):
                issues.append(f"row {r} yao_name excel={d!r} json={e.get('yao_name')!r}")
            if f is not None and str(f).strip() != e.get("yao_ci"):
                issues.append(f"row {r} yao_ci mismatch at {r}")
            if g is not None and str(g).strip() != e.get("market_judgement"):
                issues.append(f"row {r} market_judgement mismatch at {r}")
        return issues


class BaguaCalculator:
    def __init__(self, knowledge: Optional[BaguaKnowledge] = None):
        self.knowledge = knowledge

    @classmethod
    def from_json(cls, path: Path | str) -> "BaguaCalculator":
        return cls(BaguaKnowledge(path))

    def calculate(
        self,
        *,
        open_price,
        high_price,
        low_price,
        close_price,
    ) -> BaguaResult:
        o_s = format_price_2(open_price)
        h_s = format_price_2(high_price)
        l_s = format_price_2(low_price)
        c_s = format_price_2(close_price)

        o_sum = digit_sum_price(o_s)
        h_sum = digit_sum_price(h_s)
        l_sum = digit_sum_price(l_s)
        c_sum = digit_sum_price(c_s)

        upper_id = mod_map(o_sum, 8)
        lower_id = mod_map(c_sum, 8)
        yao_order = mod_map(h_sum + l_sum, 6)

        u = TRIGRAMS[upper_id]
        lo = TRIGRAMS[lower_id]

        gua_order = None
        gua_symbol = ""
        gua_name = f"{u['alias']}{lo['alias']}"
        full_name = ""
        gua_ci = ""
        core_gang = ""
        yao_ci = ""
        market_judgement = ""
        biangua = ""
        note = ""
        yao_name = ""

        entry = None
        if self.knowledge:
            entry = self.knowledge.lookup(upper_id, lower_id, yao_order)
        if entry:
            gua_order = entry.get("gua_order")
            gua_symbol = entry.get("gua_symbol") or ""
            gua_name = entry.get("gua_name") or gua_name
            full_name = entry.get("full_name") or f"{gua_symbol}{gua_name}"
            gua_ci = entry.get("gua_ci") or ""
            core_gang = entry.get("core_gang") or ""
            yao_ci = entry.get("yao_ci") or ""
            market_judgement = entry.get("market_judgement") or ""
            biangua = entry.get("biangua") or ""
            note = entry.get("note") or ""
            yao_name = entry.get("yao_name") or ""
        else:
            yao_name = {1: "初", 2: "二", 3: "三", 4: "四", 5: "五", 6: "上"}.get(
                yao_order, str(yao_order)
            )
            full_name = gua_name

        return BaguaResult(
            open_price=o_s,
            high_price=h_s,
            low_price=l_s,
            close_price=c_s,
            open_digit_sum=o_sum,
            high_digit_sum=h_sum,
            low_digit_sum=l_sum,
            close_digit_sum=c_sum,
            upper_id=upper_id,
            lower_id=lower_id,
            upper_name=u["name"],
            lower_name=lo["name"],
            upper_alias=u["alias"],
            lower_alias=lo["alias"],
            upper_symbol=u["symbol"],
            lower_symbol=lo["symbol"],
            yao_order=yao_order,
            yao_name=yao_name,
            gua_order=gua_order,
            gua_symbol=gua_symbol,
            gua_name=gua_name,
            full_name=full_name,
            gua_ci=gua_ci,
            core_gang=core_gang,
            yao_ci=yao_ci,
            market_judgement=market_judgement,
            biangua=biangua,
            note=note,
        )


def rebuild_knowledge_from_excel(xlsx_path: Path, out_json: Path) -> dict:
    """Rebuild bagua_384.json from Excel authority source."""
    import openpyxl

    xlsx_path = Path(xlsx_path)
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    sha = hashlib.sha256(xlsx_path.read_bytes()).hexdigest()
    self_map = {
        "乾为天": (1, 1),
        "坤为地": (8, 8),
        "震为雷": (4, 4),
        "艮为山": (7, 7),
        "坎为水": (6, 6),
        "离为火": (3, 3),
        "巽为风": (5, 5),
        "兑为泽": (2, 2),
    }
    alias_to_id = {v["alias"]: k for k, v in TRIGRAMS.items()}

    def parse_ul(name: str):
        if name in self_map:
            return self_map[name]
        return alias_to_id[name[0]], alias_to_id[name[1]]

    entries: List[dict] = []
    current = None
    gua_idx = 0
    for r in range(2, 386):
        vals = [ws.cell(r, c).value for c in range(1, 9)]
        gname, gci, gang, yao_pos, biangua, yao_ci, pan, note = vals
        if not gname:
            raise ValueError(f"missing hexagram name at row {r}")
        full = str(gname).strip()
        if current is None or full != current["full_name"]:
            gua_idx += 1
            m = re.match(r"^(.)(.+)$", full)
            symbol = m.group(1) if m else ""
            rest = m.group(2) if m else full
            current = {
                "gua_order": gua_idx,
                "full_name": full,
                "gua_symbol": symbol,
                "gua_name": rest,
                "gua_ci": str(gci).strip() if gci else "",
                "core_gang": str(gang).strip() if gang else "",
            }
        else:
            if gci and str(gci).strip():
                current["gua_ci"] = str(gci).strip()
            if gang and str(gang).strip():
                current["core_gang"] = str(gang).strip()

        yao = str(yao_pos).strip() if yao_pos else ""
        u, l = parse_ul(current["gua_name"])
        entries.append(
            {
                "excel_row": r,
                "gua_order": current["gua_order"],
                "gua_symbol": current["gua_symbol"],
                "gua_name": current["gua_name"],
                "full_name": current["full_name"],
                "gua_ci": current["gua_ci"],
                "core_gang": current["core_gang"],
                "yao_order": YAO_ORDER_MAP.get(yao),
                "yao_name": yao,
                "biangua": str(biangua).strip() if biangua else "",
                "yao_ci": str(yao_ci).strip() if yao_ci else "",
                "market_judgement": str(pan).strip() if pan else "",
                "note": str(note).strip() if note else "",
                "upper": u,
                "lower": l,
                "upper_name": TRIGRAMS[u]["name"],
                "lower_name": TRIGRAMS[l]["name"],
                "upper_alias": TRIGRAMS[u]["alias"],
                "lower_alias": TRIGRAMS[l]["alias"],
                "upper_symbol": TRIGRAMS[u]["symbol"],
                "lower_symbol": TRIGRAMS[l]["symbol"],
            }
        )

    # propagate group fields within each gua_order
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
        "trigrams": TRIGRAMS,
        "entries": entries,
        "count_gua": 64,
        "count_yao": 384,
    }
    out_json = Path(out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(kb, ensure_ascii=False, indent=2), encoding="utf-8")
    return kb
