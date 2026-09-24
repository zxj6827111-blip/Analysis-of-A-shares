# -*- coding: utf-8 -*-
"""EOD 链尾自动生成的「全市场数据表」（可下载）。

需求（2026-09-23）：每周最后交易日 EOD 更新跑完后，立刻产出一份可下载的
xlsx，内容为**指标筛选 sheet 之外的全部基础数据**——大盘指数（index-all）、
ETF（etf-all）、所有 A 股（stock-all，含北交所）。

实现要点：
- 直接复用导出主链 ``export_bagua_multi_period_xlsx``，``review_rules=[]``
  明示不带任何指标筛选 sheet（None 会沿用「读周五链复核 JSON 全量」的旧
  默认——那不是本功能要的内容）；
- 产物用独立前缀 ``auto_weekly_`` 落 ``storage/astock/bagua_exports/``，
  与手工导出的 ``bagua_weekly_`` 前缀分开——保留期清理只删自己生成的文件；
- 最新结果写 ``storage/astock/auto_export_state.json``（原子写），前端轮询
  ``/api/v1/bagua/export/auto/latest`` 拿状态、``/download`` 直下文件——
  与服务内 journal 无关，重启可恢复；
- 全市场重任务：调用方（CLI/EOD 链）须在 heavy-job 全局锁内执行（契约 §7）。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..config import AStockConfig
from ..data.io_util import atomic_write_json

AUTO_EXPORT_SCHEMA = 1

#: 自动导出文件名前缀（保留期清理只认它，手工导出的 bagua_weekly_* 不动）
AUTO_FILE_PREFIX = "auto_weekly_"

#: 保留最近多少份自动导出（磁盘保护；env ASTOCK_AUTO_EXPORT_KEEP 可覆盖）
DEFAULT_KEEP = 4

#: EOD 链/CLI 的重任务待办键前缀（api._heavy_job_command 依此前缀映射补跑命令）
PENDING_TASK_PREFIX = "auto_export_"


def auto_export_state_path(cfg: AStockConfig) -> Path:
    return Path(cfg.storage_root) / "auto_export_state.json"


def auto_export_dir(cfg: AStockConfig) -> Path:
    return Path(cfg.storage_root) / "bagua_exports"


def load_auto_export_state(cfg: AStockConfig) -> Dict[str, Any]:
    """读最新自动导出状态；缺失/损坏 → 空 dict（调用方各字段自行兜底）。"""
    try:
        p = auto_export_state_path(cfg)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_auto_export_state(cfg: AStockConfig, state: Dict[str, Any]) -> Path:
    """atomic 写最新状态（含 schema 版本，旧文件可直接覆盖）。"""
    payload = {"schema": AUTO_EXPORT_SCHEMA, **state}
    path = auto_export_state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)
    return path


def _keep_count(default: int = DEFAULT_KEEP) -> int:
    import os

    try:
        return max(1, int(os.environ.get("ASTOCK_AUTO_EXPORT_KEEP", str(default))))
    except (TypeError, ValueError):
        return default


def _prune_old_exports(export_root: Path, *, keep: int) -> List[str]:
    """按 mtime 只保留最近的 ``keep`` 份自动导出，返回被删文件名列表。"""
    removed: List[str] = []
    try:
        files = sorted(
            (
                p
                for p in Path(export_root).glob(f"{AUTO_FILE_PREFIX}*.xlsx")
                if p.is_file()
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:  # noqa: BLE001 — 清理失败不影响导出本体
        return removed
    for p in files[max(0, keep):]:
        try:
            p.unlink()
            removed.append(p.name)
        except OSError:  # noqa: BLE001
            continue
    return removed


def _sheet_counts(path: Path) -> Dict[str, int]:
    """读成品 workbook 的 sheet -> 数据行数（不含表头/说明区的近似口径）。

    用途是状态上报（页面上"指数 N 条 / ETF M 条 / 股票 K 条"），不是审计口径。
    """
    counts: Dict[str, int] = {}
    try:
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True)
        try:
            for ws in wb.worksheets:
                # max_row 含说明区/表头；负数不出现，宽松减 1 仅作展示
                counts[ws.title] = max(0, int(ws.max_row or 0))
        finally:
            wb.close()
    except Exception:  # noqa: BLE001
        counts = {}
    return counts


def run_auto_export(
    cfg: AStockConfig,
    *,
    date: Optional[Union[str, int]] = None,
    keep: Optional[int] = None,
) -> Dict[str, Any]:
    """生成最新一份全市场数据表并回写状态文件。

    返回 ``{status: done|error, path, filename, export_date, sheets, ...}``；
    失败时 ``status=error`` 且带 ``error``（状态文件同样记录，绝不静默）。
    """
    from .bagua_query import export_bagua_multi_period_xlsx

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    save_auto_export_state(
        cfg,
        {"status": "running", "started_at": started, "export_date": date},
    )
    info: Dict[str, Any] = {}
    try:
        stamp = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        asof = int(str(date).replace("-", "")) if date else int(time.strftime("%Y%m%d"))
        export_root = auto_export_dir(cfg)
        export_root.mkdir(parents=True, exist_ok=True)
        out = export_root / f"{AUTO_FILE_PREFIX}{asof}_{stamp}.xlsx"
        path = export_bagua_multi_period_xlsx(
            cfg,
            date=asof,
            periods=None,  # 导出函数内部强制 WEEK+MONTH（weekly_analysis 版式）
            adjust="tushare_qfq",
            codes=None,
            all_stocks=True,
            limit=None,
            path=out,
            review_rules=[],  # 空列表 = 明确不带任何指标筛选 sheet
            info_out=info,
        )
        path = Path(path)
        counts = _sheet_counts(path)
        removed = _prune_old_exports(export_root, keep=_keep_count() if keep is None else max(1, int(keep)))
        finished = time.strftime("%Y-%m-%d %H:%M:%S")
        size_bytes = path.stat().st_size if path.exists() else 0
        state = {
            "status": "done",
            "export_date": asof,
            "query_date": info.get("query_date"),
            "started_at": started,
            "finished_at": finished,
            "elapsed_sec": round(time.time() - t0, 1),
            "path": str(path),
            "filename": path.name,
            "size_bytes": size_bytes,
            "sheets": counts,
            "sheet_names": list(counts.keys()),
            "includes_signal_sheets": False,
            "pruned": removed,
            "keep": _keep_count() if keep is None else max(1, int(keep)),
            "error": None,
        }
        save_auto_export_state(cfg, state)
        return state
    except Exception as e:  # noqa: BLE001 — 链尾附属产物失败不能拖垮同步链，但如实落账
        state = {
            "status": "error",
            "export_date": date,
            "started_at": started,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_sec": round(time.time() - t0, 1),
            "path": None,
            "filename": None,
            "size_bytes": 0,
            "sheets": {},
            "includes_signal_sheets": False,
            "error": f"{type(e).__name__}: {e}",
        }
        save_auto_export_state(cfg, state)
        return state
