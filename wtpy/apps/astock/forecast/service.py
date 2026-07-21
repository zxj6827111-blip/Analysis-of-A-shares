"""Forecast service: paths, KB, weekly snapshots, query & export."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..config import AStockConfig
from .kb_loader import (
    ForecastKnowledgeBase,
    import_xlsx_to_kb,
    save_version_copy,
    seed_from_existing_json,
)
from .matcher import GuaMatch, match_period
from .name_norm import normalize_stock_code
from .search_index import StockSearchIndex
from .weekly_importer import import_weekly_xlsx, load_snapshot_stocks


class ForecastService:
    def __init__(self, cfg: AStockConfig):
        self.cfg = cfg
        self.root = Path(cfg.forecast_root)
        self.kb_path = Path(cfg.forecast_kb_path)
        self.versions_dir = self.root / "kb" / "versions"
        self.weekly_root = Path(cfg.forecast_weekly_dir)
        self.exports_dir = Path(cfg.forecast_exports_dir)
        self.yao_index_base = int(getattr(cfg, "forecast_yao_index_base", 0) or 0)

        self._kb: Optional[ForecastKnowledgeBase] = None
        self._search = StockSearchIndex()
        self._active_week: Optional[str] = None
        self._stocks_by_code: Dict[str, dict] = {}
        self._stock_rows: List[dict] = []
        self._week_meta: Optional[dict] = None

        self.ensure_dirs()
        self._ensure_default_kb()
        self.reload()


    def _ensure_default_kb(self) -> None:
        """Load default 384-爻简判 once; no need for weekly re-import.

        Priority:
        1. existing forecast_kb_path with operation_signal coverage
        2. 指标/*操作信号*.xlsx or *384*.xlsx
        3. seed from backtest bagua_384.json
        """
        def _has_signals(path: Path) -> bool:
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                ents = data.get("entries") or []
                return any(str(e.get("operation_signal") or "").strip() for e in ents[:20])
            except Exception:
                return False

        kb_ok = Path(self.kb_path).exists() and Path(self.kb_path).stat().st_size > 100
        if kb_ok and _has_signals(self.kb_path):
            return

        # Prefer indicator workbook that includes 操作信号
        try:
            ind = Path(self.cfg.indicator_dir) if self.cfg.indicator_dir else None
            if ind and ind.exists():
                cands = list(ind.glob("*操作信号*.xlsx")) + list(ind.glob("*384*.xlsx"))
                # unique preserve order
                seen = set()
                ordered = []
                for c in cands:
                    if c.resolve() in seen:
                        continue
                    seen.add(c.resolve())
                    ordered.append(c)
                ordered.sort(key=lambda x: (("操作信号" not in x.name), -x.stat().st_mtime))
                if ordered:
                    import_xlsx_to_kb(ordered[0], out_json=self.kb_path, version_id="default_xlsx")
                    if _has_signals(self.kb_path) or Path(self.kb_path).exists():
                        return
        except Exception:
            pass

        if kb_ok:
            return
        try:
            src = Path(self.cfg.bagua_json) if self.cfg.bagua_json else None
            if src and src.exists():
                seed_from_existing_json(src, self.kb_path, version_id="default_seed")
        except Exception:
            pass

    def ensure_dirs(self) -> None:
        for p in [
            self.root,
            self.kb_path.parent,
            self.versions_dir,
            self.weekly_root / "raw",
            self.weekly_root / "snapshots",
            self.exports_dir,
        ]:
            Path(p).mkdir(parents=True, exist_ok=True)

    # ----- reload -----
    def reload(self) -> None:
        self._load_kb()
        self._load_active_week()

    def _load_kb(self) -> None:
        if self.kb_path.exists():
            self._kb = ForecastKnowledgeBase.from_json_path(self.kb_path)
        else:
            self._kb = None

    def _load_active_week(self) -> None:
        index = self._weekly_index()
        wk = index.get("active_week_key")
        self._active_week = wk
        self._stocks_by_code.clear()
        self._stock_rows = []
        self._week_meta = None
        self._search.clear()
        if not wk:
            return
        try:
            meta, rows = load_snapshot_stocks(self.weekly_root, wk)
        except FileNotFoundError:
            return
        self._week_meta = meta
        self._stock_rows = rows
        for r in rows:
            c = normalize_stock_code(r.get("code6") or r.get("code"))
            if c:
                self._stocks_by_code[c] = r
        self._search.rebuild(rows)

    def _weekly_index(self) -> dict:
        path = self.weekly_root / "index.json"
        if not path.exists():
            return {"weeks": {}, "active_week_key": None}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"weeks": {}, "active_week_key": None}
        w = data.get("weeks")
        if isinstance(w, list):
            # normalize list -> dict by week_key
            d = {}
            for item in w:
                if isinstance(item, dict) and item.get("week_key"):
                    d[str(item["week_key"])] = item
            data["weeks"] = d
        elif not isinstance(w, dict):
            data["weeks"] = {}
        return data

    def _save_weekly_index(self, index: dict) -> None:
        path = self.weekly_root / "index.json"
        path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    # ----- health -----
    def health(self) -> dict:
        kb = self._kb
        stock_n = sum(1 for r in self._stock_rows if (r.get("kind") or "stock") != "etf")
        etf_n = sum(1 for r in self._stock_rows if (r.get("kind") or "") == "etf")
        meta = self._week_meta or {}
        if etf_n == 0 and meta.get("etf_count"):
            # older snapshots may lack kind; use meta when merge size matches
            meta_etf = int(meta.get("etf_count") or 0)
            meta_stk = int(meta.get("stock_count") or 0)
            if meta_stk and stock_n >= meta_stk and len(self._stock_rows) >= meta_stk + meta_etf:
                etf_n = meta_etf
                stock_n = meta_stk
        return {
            "ok": True,
            "forecast_root": str(self.root),
            "kb_loaded": kb is not None,
            "kb_path": str(self.kb_path),
            "kb_version": (kb.version_id if kb else None),
            "kb_entry_count": (len(kb.entries) if kb else 0),
            "kb_source_file": (kb.source_file if kb else None),
            "active_week_key": self._active_week,
            "stock_count": stock_n,
            "etf_count": etf_n,
            "instrument_count": len(self._stock_rows),
            "week_meta": self._week_meta,
            "yao_index_base": self.yao_index_base,
        }

    # ----- KB -----
    def import_kb_xlsx(self, path: Path | str, *, activate: bool = True) -> dict:
        path = Path(path)
        version_id = time.strftime("%Y%m%d_%H%M%S")
        tmp_out = self.versions_dir / f"{version_id}.json"
        kb = import_xlsx_to_kb(path, out_json=tmp_out, version_id=version_id)
        # import_xlsx already wrote tmp_out; also write meta (avoid copy lock on Windows)
        meta = {
            "version_id": version_id,
            "path": str(tmp_out),
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "count_yao": kb.get("count_yao") or len(kb.get("entries") or []),
        }
        (self.versions_dir / f"{version_id}.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if activate:
            text = tmp_out.read_text(encoding="utf-8")
            self.kb_path.parent.mkdir(parents=True, exist_ok=True)
            self.kb_path.write_text(text, encoding="utf-8")
            self._load_kb()
        return {
            "version_id": version_id,
            "count_yao": kb.get("count_yao") or len(kb.get("entries") or []),
            "count_gua": kb.get("count_gua"),
            "source_file": kb.get("source_file"),
            "activated": activate,
            "kb_path": str(self.kb_path),
        }

    def seed_kb_from_backtest(self) -> dict:
        """Bootstrap forecast KB from project bagua_384.json if present."""
        src = Path(self.cfg.bagua_json) if self.cfg.bagua_json else None
        if not src or not src.exists():
            raise FileNotFoundError("backtest bagua_json missing")
        version_id = "seed_" + time.strftime("%Y%m%d_%H%M%S")
        data = seed_from_existing_json(src, self.kb_path, version_id=version_id)
        save_version_copy(self.kb_path, self.versions_dir, version_id)
        self._load_kb()
        return {
            "version_id": version_id,
            "count_yao": len(data.get("entries") or []),
            "seeded_from": str(src),
        }

    def list_kb_versions(self) -> List[dict]:
        out = []
        if not self.versions_dir.exists():
            return out
        for p in sorted(self.versions_dir.glob("*.meta.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    def activate_kb_version(self, version_id: str) -> dict:
        src = self.versions_dir / f"{version_id}.json"
        if not src.exists():
            raise FileNotFoundError(version_id)
        self.kb_path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        # patch version_id in file
        data = json.loads(self.kb_path.read_text(encoding="utf-8"))
        data["version_id"] = version_id
        self.kb_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._load_kb()
        return {"version_id": version_id, "activated": True}

    # ----- weekly -----
    def upload_weekly(self, path: Path | str, *, activate: bool = True) -> dict:
        meta = import_weekly_xlsx(
            path, weekly_root=self.weekly_root, activate=activate
        )
        self._load_active_week()
        return meta

    def list_weeks(self) -> dict:
        """Return imported weeks for UI dropdown (always safe)."""
        try:
            index = self._weekly_index()
        except Exception:
            index = {"weeks": {}, "active_week_key": None}
        raw_weeks = index.get("weeks") or {}
        if isinstance(raw_weeks, dict):
            weeks = [v for v in raw_weeks.values() if isinstance(v, dict)]
        elif isinstance(raw_weeks, list):
            weeks = [v for v in raw_weeks if isinstance(v, dict)]
        else:
            weeks = []
        weeks_sorted = sorted(
            weeks,
            key=lambda w: str(w.get("imported_at") or w.get("week_key") or ""),
            reverse=True,
        )
        enriched = []
        for w in weeks_sorted:
            item = dict(w)
            wk = str(item.get("week_key") or "")
            try:
                meta_path = item.get("meta_path")
                if meta_path and Path(meta_path).exists():
                    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
                    if isinstance(meta, dict):
                        for k in (
                            "source_filename",
                            "stock_count",
                            "etf_count",
                            "instrument_count",
                            "week_end",
                            "imported_at",
                        ):
                            if item.get(k) in (None, "") and meta.get(k) is not None:
                                item[k] = meta.get(k)
            except Exception:
                pass
            fn = item.get("source_filename") or ""
            sc = item.get("stock_count")
            ec = item.get("etf_count")
            label = wk or "unknown"
            if fn:
                label += f" · {fn}"
            bits = []
            if sc is not None:
                bits.append(f"{sc}股")
            if ec is not None:
                bits.append(f"{ec}ETF")
            if bits:
                label += " · " + "+".join(bits)
            item["label"] = label
            enriched.append(item)
        return {
            "active_week_key": index.get("active_week_key"),
            "weeks": enriched,
            "count": len(enriched),
        }


    def activate_week(self, week_key: str) -> dict:
        index = self._weekly_index()
        if week_key not in (index.get("weeks") or {}):
            raise FileNotFoundError(week_key)
        index["active_week_key"] = week_key
        self._save_weekly_index(index)
        self._load_active_week()
        etf_n = sum(1 for r in self._stock_rows if (r.get("kind") or "") == "etf")
        return {
            "active_week_key": week_key,
            "stock_count": len(self._stock_rows) - etf_n,
            "etf_count": etf_n,
            "instrument_count": len(self._stock_rows),
        }

    # ----- query -----
    def search(self, q: str, limit: int = 20) -> List[dict]:
        return [h.to_dict() for h in self._search.search(q, limit=limit)]

    def _match_row(self, row: dict) -> Dict[str, Any]:
        week_m = match_period(
            self._kb,
            period="week",
            ben_raw=row.get("本周周线-本卦"),
            bian_raw=row.get("本周周线-变卦"),
            yao_index_base=self.yao_index_base,
        )
        month_m = match_period(
            self._kb,
            period="month",
            ben_raw=row.get("上月月线-本卦"),
            bian_raw=row.get("上月月线-变卦"),
            yao_index_base=self.yao_index_base,
        )
        return {
            "week": week_m.to_dict(),
            "month": month_m.to_dict(),
        }

    def quote(self, q: str) -> dict:
        """Single-stock forecast by code or search query."""
        q = (q or "").strip()
        if not q:
            return {
                "found": False,
                "tips": ["请输入股票代码或名称"],
                "active_week_key": self._active_week,
            }

        row = None
        code = normalize_stock_code(q) if q.isdigit() or normalize_stock_code(q).isdigit() else ""
        # try direct code
        c_try = normalize_stock_code(q)
        if c_try in self._stocks_by_code:
            row = self._stocks_by_code[c_try]
        if row is None:
            hits = self._search.search(q, limit=5)
            if len(hits) == 1:
                row = self._stocks_by_code.get(hits[0].code6)
            elif len(hits) > 1:
                return {
                    "found": False,
                    "ambiguous": True,
                    "candidates": [h.to_dict() for h in hits],
                    "tips": ["匹配到多只股票，请选择更精确的代码或名称"],
                    "active_week_key": self._active_week,
                }

        if row is None:
            return {
                "found": False,
                "tips": ["未在本周周报中找到该股票/ETF"],
                "active_week_key": self._active_week,
                "query": q,
            }

        matches = self._match_row(row)
        tips: List[str] = []
        for side in ("week", "month"):
            tips.extend(matches[side].get("tips") or [])
        kind = str(row.get("kind") or "stock").lower()
        if kind not in ("stock", "etf"):
            kind = "etf" if "etf" in str(row.get("name") or "").lower() else "stock"

        return {
            "found": True,
            "active_week_key": self._active_week,
            "code": row.get("code6") or normalize_stock_code(row.get("code")),
            "name": row.get("name"),
            "kind": kind,
            "industry": row.get("industry") or row.get("industy"),
            "week_key": row.get("week_key") or self._active_week,
            "week_end": row.get("week_end"),
            "quote": {
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "ret_w_pct": row.get("ret_w(%)"),
                "amp_w_pct": row.get("amp_w(%)"),
                "日柱": row.get("日柱"),
                "本周周线-后天卦-开": row.get("本周周线-后天卦-开"),
                "本周周线-后天卦-收": row.get("本周周线-后天卦-收"),
                "本周周线-组合": row.get("本周周线-组合"),
            },
            "week_match": matches["week"],
            "month_match": matches["month"],
            "tips": tips,
            "kb_loaded": self._kb is not None,
        }

    def batch_query(
        self,
        codes: Optional[Sequence[str]] = None,
        *,
        all_stocks: bool = False,
        limit: Optional[int] = None,
    ) -> dict:
        rows: List[dict] = []
        if all_stocks:
            rows = list(self._stock_rows)
        else:
            codes = codes or []
            for c in codes:
                r = self._stocks_by_code.get(normalize_stock_code(c))
                if r:
                    rows.append(r)
                else:
                    rows.append(
                        {
                            "code6": normalize_stock_code(c),
                            "name": "",
                            "_missing": True,
                        }
                    )
        if limit is not None:
            rows = rows[: int(limit)]

        results = []
        for r in rows:
            if r.get("_missing"):
                results.append(
                    {
                        "found": False,
                        "code": r.get("code6"),
                        "name": "",
                        "tips": ["未在本周周报中找到该股票/ETF"],
                    }
                )
                continue
            m = self._match_row(r)
            results.append(
                {
                    "found": True,
                    "code": r.get("code6") or normalize_stock_code(r.get("code")),
                    "name": r.get("name"),
                    "kind": r.get("kind") or "stock",
                    "industry": r.get("industry") or r.get("industy"),
                    "week_key": r.get("week_key") or self._active_week,
                    "week_end": r.get("week_end"),
                    "ret_w_pct": r.get("ret_w(%)"),
                    "week_match": m["week"],
                    "month_match": m["month"],
                }
            )
        return {
            "active_week_key": self._active_week,
            "count": len(results),
            "results": results,
        }

    def export_xlsx(self, path: Optional[Path] = None) -> Path:
        import openpyxl
        from openpyxl.styles import Font

        if not self._stock_rows:
            raise ValueError("no active weekly snapshot")

        path = Path(
            path
            or (
                self.exports_dir
                / f"forecast_{self._active_week or 'na'}_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
            )
        )
        path.parent.mkdir(parents=True, exist_ok=True)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "forecast"
        headers = [
            "code",
            "name",
            "kind",
            "industry",
            "week_key",
            "week_end",
            "ret_w(%)",
            "本周周线-本卦",
            "本周周线-变卦",
            "week_yao_order",
            "week_match_status",
            "week_judgement",
            "上月月线-本卦",
            "上月月线-变卦",
            "month_yao_order",
            "month_match_status",
            "month_judgement",
        ]
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)

        for r in self._stock_rows:
            m = self._match_row(r)
            ws.append(
                [
                    r.get("code6") or normalize_stock_code(r.get("code")),
                    r.get("name"),
                    r.get("kind") or "stock",
                    r.get("industry") or r.get("industy"),
                    r.get("week_key") or self._active_week,
                    r.get("week_end"),
                    r.get("ret_w(%)"),
                    r.get("本周周线-本卦"),
                    r.get("本周周线-变卦"),
                    m["week"].get("yao_order"),
                    m["week"].get("match_status"),
                    m["week"].get("judgement"),
                    r.get("上月月线-本卦"),
                    r.get("上月月线-变卦"),
                    m["month"].get("yao_order"),
                    m["month"].get("match_status"),
                    m["month"].get("judgement"),
                ]
            )
        wb.save(path)
        return path
