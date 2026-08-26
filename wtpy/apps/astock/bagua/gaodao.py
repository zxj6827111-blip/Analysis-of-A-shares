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


@lru_cache(maxsize=8)
def _canonical(path_str: str) -> str:
    """记忆化 Path.resolve()。

    实测 Windows 上 resolve() 约 270µs，占单次索引取用开销的 96%
    （stat 仅 ~4µs、索引对象构造 ~0.6µs）。全市场导出会按
    「股票数 × 周期数」量级调用（5400 股 × WEEK/MONTH ≈ 1.08 万次），
    不记忆化会白耗约 2.3 秒。路径字符串在进程内稳定，
    按字符串缓存即可；mtime 仍每次 stat，缓存失效语义不受影响。
    """
    return str(Path(path_str).resolve())


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
    return _load_raw(_canonical(str(path)), mtime)


def invalidate_gaodao_cache() -> None:
    """清空 sidecar 缓存（重建数据文件后调用）。"""
    _load_raw.cache_clear()
    _canonical.cache_clear()


class GaodaoIndex:
    """一次加载、多次查询的轻量索引。

    列表类接口（384 爻目录、卦象目录）需要逐爻查断语，
    若每爻都走 load_gaodao 会重复 stat 文件；用索引对象只加载一次。
    sidecar 不可用时索引为空，所有查询返回空值（fail-open）。
    """

    __slots__ = ("_by_state_id", "_primary")

    def __init__(self, data: Optional[dict]) -> None:
        if isinstance(data, dict):
            self._by_state_id = data.get("by_state_id") or {}
            pol = data.get("policy") or {}
            prim = pol.get("primary")
            self._primary = frozenset(str(x) for x in prim) if isinstance(prim, list) else frozenset()
        else:
            self._by_state_id = {}
            self._primary = frozenset()

    def __bool__(self) -> bool:
        return bool(self._by_state_id)

    def get(self, state_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not state_id:
            return None
        item = self._by_state_id.get(str(state_id))
        if not isinstance(item, dict) or not item.get("text"):
            return None
        return item

    def is_primary(self, category: Optional[str]) -> bool:
        """类别是否属营商类；sidecar 无 policy 时保守视为营商（不加后缀）。"""
        if not category or not self._primary:
            return True
        return str(category) in self._primary

    def is_fallback(self, state_id: Optional[str]) -> bool:
        """该爻的断语是否来自兜底类别（时运/功名等非营商类）。

        供前端直接判断是否要标注出处，避免前端各自硬编码营商类别名
        导致与 sidecar 的 policy.primary 失步。无断语的爻返回 False。
        """
        item = self.get(state_id)
        if not item:
            return False
        return not self.is_primary(str(item.get("category") or ""))

    def category(self, state_id: Optional[str]) -> str:
        item = self.get(state_id)
        return str((item or {}).get("category") or "")

    def display(self, state_id: Optional[str]) -> str:
        """展示串：非营商类别（时运/功名兜底）追加类别后缀，避免口径误读。"""
        item = self.get(state_id)
        if not item:
            return ""
        text = str(item.get("text") or "").strip()
        if not text:
            return ""
        cat = str(item.get("category") or "").strip()
        if not cat or self.is_primary(cat):
            return text
        return f"{text}{_FALLBACK_SUFFIX_FMT.format(category=cat)}"


def gaodao_index(cfg: Optional[AStockConfig] = None) -> GaodaoIndex:
    """构造一次性索引对象（列表类接口用）。"""
    return GaodaoIndex(load_gaodao(cfg))


def gaodao_for_state(
    state_id: Optional[str], cfg: Optional[AStockConfig] = None
) -> Optional[Dict[str, Any]]:
    """按 state_id（如 "01-1"）取断语，返回 {text, category, gua_name, yao_name}。

    未命中（含 5 个原书无占断的爻）或 sidecar 不可用时返回 None。
    """
    return gaodao_index(cfg).get(state_id)


def gaodao_display(
    state_id: Optional[str], cfg: Optional[AStockConfig] = None
) -> str:
    """取用于展示的断语串：非营商类别（时运/功名兜底）追加类别后缀。"""
    return gaodao_index(cfg).display(state_id)


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
    return gaodao_index(cfg).is_primary(category)


def gaodao_is_fallback(
    state_id: Optional[str], cfg: Optional[AStockConfig] = None
) -> bool:
    """该爻断语是否取自兜底类别（时运/功名等）。无断语或 sidecar 不可用返回 False。"""
    return gaodao_index(cfg).is_fallback(state_id)


def coverage_label(cfg: Optional[AStockConfig] = None) -> str:
    """人读的覆盖度摘要串，写入导出 Excel 的 meta sheet。

    counts 可能不完整（手工改过的 sidecar、旧版 schema），逐项做缺失降级，
    避免把 None 直接格式化进给人看的表格里（如 "1/None（营商 None…）"）。
    """
    cov = gaodao_coverage(cfg)
    if not cov:
        return "sidecar 缺失，高岛列为空"
    total = cov.get("total")
    state_total = cov.get("state_total")
    primary = cov.get("primary")
    fallback = cov.get("fallback")
    missing = cov.get("missing")
    if total is None and state_total is None:
        return "sidecar 未记录 counts，覆盖度未知"
    head = "{}/{}".format(
        total if total is not None else "-",
        state_total if state_total is not None else "-",
    )
    if primary is None or fallback is None or missing is None:
        return f"{head}（sidecar 未记录明细）"
    return f"{head}（营商 {primary} + 兜底 {fallback}，缺失 {missing}）"


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
