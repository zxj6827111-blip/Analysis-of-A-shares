# -*- coding: utf-8 -*-
"""《高岛易断》营商断语 sidecar 读取模块（唯一数据入口）。

设计要点：
    1. sidecar（bagua_gaodao.json）与 384 爻知识库（bagua_384.json）物理隔离。
       知识库头部有 source_sha256 绑定权威 Excel，且 reimport_excel() 会整体重建，
       所以高岛字段必须旁挂，不能写进知识库。
    2. fail-open：文件缺失/损坏一律返回 None/空，绝不抛异常。
       高岛断语只是展示层增强，缺失时前端仍有 market_judgement 兜底，
       不允许因为一个可选数据文件让卦象查询整体不可用。
    3. 缓存策略对齐 service/gua.py::_load_raw —— lru_cache 以 (path, mtime) 为键，
       文件被重新生成后 mtime 变化即自动失效。

生成/重建 sidecar::

    python -X utf8 scripts/build_gaodao_sidecar.py
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import AStockConfig, get_default_config

# 非营商类别（时运/功名兜底）在展示时加后缀，避免让用户误以为是营商断语
_FALLBACK_SUFFIX_FMT = "（{category}）"


def gaodao_path(cfg: Optional[AStockConfig] = None) -> Path:
    """解析 sidecar 路径；配置缺失时回落到包内默认位置。"""
    cfg = cfg or get_default_config()
    p = getattr(cfg, "bagua_gaodao_json", None)
    if p is None:
        p = Path(__file__).resolve().parent / "bagua_gaodao.json"
    return Path(p)


@lru_cache(maxsize=4)
def _load_raw(path_str: str, mtime: float) -> Optional[dict]:
    """按 (路径, mtime) 缓存读取；任何异常都降级为 None（fail-open）。"""
    import json

    try:
        data = json.loads(Path(path_str).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("by_state_id"), dict):
        return None
    return data


def load_gaodao(cfg: Optional[AStockConfig] = None) -> Optional[dict]:
    """加载 sidecar 全量数据；文件不存在或格式不对返回 None。"""
    path = gaodao_path(cfg)
    try:
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        return None
    if not mtime:
        return None
    return _load_raw(str(path.resolve()), mtime)


def invalidate_gaodao_cache() -> None:
    """清空 sidecar 缓存（重建数据文件后调用）。"""
    _load_raw.cache_clear()


def gaodao_for_state(
    state_id: Optional[str], cfg: Optional[AStockConfig] = None
) -> Optional[Dict[str, Any]]:
    """按 state_id（如 "01-1"）取断语，返回 {text, category, gua_name, yao_name}。

    未命中（含 5 个原书无占断的爻）或 sidecar 不可用时返回 None。
    """
    if not state_id:
        return None
    data = load_gaodao(cfg)
    if not data:
        return None
    item = data["by_state_id"].get(str(state_id))
    if not isinstance(item, dict) or not item.get("text"):
        return None
    return item


def gaodao_display(
    state_id: Optional[str], cfg: Optional[AStockConfig] = None
) -> str:
    """取用于展示的断语串：非营商类别（时运/功名兜底）追加类别后缀。"""
    item = gaodao_for_state(state_id, cfg)
    if not item:
        return ""
    text = str(item.get("text") or "").strip()
    if not text:
        return ""
    if is_primary_category(item.get("category"), cfg):
        return text
    cat = str(item.get("category") or "").strip()
    return f"{text}{_FALLBACK_SUFFIX_FMT.format(category=cat)}" if cat else text


def primary_categories(cfg: Optional[AStockConfig] = None) -> List[str]:
    """营商类别名列表（sidecar 缺失时返回空列表）。"""
    data = load_gaodao(cfg)
    if not data:
        return []
    pol = data.get("policy") or {}
    prim = pol.get("primary")
    return [str(x) for x in prim] if isinstance(prim, list) else []


def is_primary_category(
    category: Optional[str], cfg: Optional[AStockConfig] = None
) -> bool:
    """判断类别是否属于营商类。sidecar 不可用时保守视为营商（不加后缀）。"""
    if not category:
        return True
    prim = primary_categories(cfg)
    if not prim:
        return True
    return str(category) in prim


def gaodao_coverage(cfg: Optional[AStockConfig] = None) -> Optional[Dict[str, Any]]:
    """覆盖度摘要，供 API 返回给前端提示（sidecar 不可用返回 None）。"""
    data = load_gaodao(cfg)
    if not data:
        return None
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    return {
        "source_file": data.get("source_file") or "",
        "source_sha256": data.get("source_sha256") or "",
        "extractor_version": data.get("extractor_version") or "",
        "generated_at": data.get("generated_at") or "",
        "total": counts.get("total"),
        "primary": counts.get("primary"),
        "fallback": counts.get("fallback"),
        "missing": counts.get("missing"),
        "state_total": counts.get("state_total"),
        "missing_state_ids": list(data.get("missing_state_ids") or []),
        "policy": data.get("policy") or {},
    }
