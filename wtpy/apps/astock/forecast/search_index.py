"""Stock search index: code / name / pinyin initials / full pinyin."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .name_norm import is_numeric_query, normalize_stock_code

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:  # pragma: no cover
    lazy_pinyin = None  # type: ignore
    Style = None  # type: ignore


@dataclass
class SearchHit:
    code6: str
    name: str
    score: float

    def to_dict(self) -> dict:
        return {"code": self.code6, "name": self.name, "score": self.score}


class StockSearchIndex:
    def __init__(self) -> None:
        self._by_code: Dict[str, dict] = {}
        self._items: List[dict] = []

    def clear(self) -> None:
        self._by_code.clear()
        self._items.clear()

    def rebuild(self, rows: Sequence[dict]) -> int:
        self.clear()
        for r in rows:
            code = normalize_stock_code(r.get("code6") or r.get("code"))
            name = str(r.get("name") or "").strip()
            if not code:
                continue
            initials, full_py = _pinyin_fields(name)
            item = {
                "code6": code,
                "name": name,
                "initials": initials,
                "pinyin": full_py,
                "row": r,
            }
            self._by_code[code] = item
            self._items.append(item)
        return len(self._items)

    def get(self, code: str) -> Optional[dict]:
        return self._by_code.get(normalize_stock_code(code))

    def search(self, q: str, limit: int = 20) -> List[SearchHit]:
        q = (q or "").strip()
        if not q:
            return []
        limit = max(1, min(int(limit), 50))
        hits: List[SearchHit] = []

        if is_numeric_query(q):
            code = normalize_stock_code(q)
            if code in self._by_code:
                it = self._by_code[code]
                return [SearchHit(it["code6"], it["name"], 100.0)]
            # prefix
            for it in self._items:
                if it["code6"].startswith(code) or it["code6"].endswith(q):
                    hits.append(SearchHit(it["code6"], it["name"], 80.0))
            hits.sort(key=lambda h: (-h.score, h.code6))
            return hits[:limit]

        q_lower = q.lower()
        q_compact = q_lower.replace(" ", "")
        for it in self._items:
            name = it["name"]
            score = 0.0
            if name == q:
                score = 95.0
            elif q in name:
                score = 70.0 + min(20.0, 20.0 * len(q) / max(len(name), 1))
            initials = it["initials"]
            pinyin = it["pinyin"]
            if initials and (q_compact == initials or initials.startswith(q_compact)):
                score = max(score, 85.0 if q_compact == initials else 75.0)
            if pinyin and (q_compact == pinyin or pinyin.startswith(q_compact)):
                score = max(score, 80.0 if q_compact == pinyin else 72.0)
            if score > 0:
                hits.append(SearchHit(it["code6"], name, score))
        hits.sort(key=lambda h: (-h.score, h.code6))
        return hits[:limit]


def _pinyin_fields(name: str) -> tuple:
    if not name:
        return "", ""
    if lazy_pinyin is None:
        return "", ""
    try:
        initials = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER)).lower()
        full = "".join(lazy_pinyin(name, style=Style.NORMAL)).lower()
        return initials, full
    except Exception:
        return "", ""
