# -*- coding: utf-8 -*-
"""快照层：全规则预筛结果的可持久化身份（阶段 1）。

职责（与 run_weekly_review 的关系）：
- run_weekly_review 保持"计算引擎"不变（含 persist 的 review_{asof}.json
  兼容层）；本模块在它之上增加**不可变快照**：
  ① 快照组装：把一次复核的逐规则结果转成契约 §0/§1 结构
     （universe_codes 完整清单 + 逐规则 matched/failed_codes/no_data_codes
     + 逐规则 status ok/partial/error）。
  ② 发布：O_EXCL 建文件 → 门槛判定 → 索引指针（契约 §6；后台重试/
     backfill/recompute 不自动替换）。
  ③ 读取：cache-first 服务筛选请求（子集校验 + any/all 严格组合语义，
     契约 §规则组②）。

数据来源有两种：全量跑（周五链/回填）与子集现算（网页筛选）。
只有全量全规则的快照才发布；子集现算**只读**快照，不写。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import AStockConfig
from . import screen_contract as sc
from .indicator_review import DEFAULT_REVIEW_RULES

logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA_VERSION = "1"


# ---------------------------------------------------------------------------
# 1. 可筛选规则枚举（ctx-free：CLI 周五链与网页筛选共用同一谓词）
# ---------------------------------------------------------------------------


def list_screenable_rule_ids(cfg: AStockConfig) -> List[str]:
    """全部可执行筛选规则 ID（screen_rule_reasons 谓词的 ctx-free 变体）。

    谓词与 screening.screen_rule_reasons 完全一致（同一函数），仅数据源
    换成 ctx-free 的注册表直读。DEFAULT_REVIEW_RULES 强制纳入（与
    list_screen_rules 的"可见 ∪ 预置"口径一致，保证周五链产物至少含
    导出依赖的两条预置规则）。
    """
    from ..indicators.registry import IndicatorRegistry
    from .indicator_review import user_registry_file
    from .screening import screen_rule_reasons

    reg = IndicatorRegistry.bootstrap(
        cfg.indicator_dir,
        cfg.mapping_path,
        user_registry_path=user_registry_file(cfg),
    )
    out: List[str] = []
    for spec in reg.list():
        pub = _spec_public(spec)
        if screen_rule_reasons(pub) is None:
            out.append(str(spec.id))
    for rid, _sheet in DEFAULT_REVIEW_RULES:
        if rid not in out:
            try:
                spec = reg.get(rid)
                if spec.compile_status == "ready":
                    out.append(rid)
            except KeyError:
                continue  # 本机公式目录没有该预置规则（如 CI fixture 环境）
    return out


def _spec_public(spec) -> Dict[str, Any]:
    """把 IndicatorSpec 转成 screen_rule_reasons 需要的 public dict。"""
    return {
        "archived": bool(getattr(spec, "parameters", {}).get("archived"))
        if isinstance(getattr(spec, "parameters", None), dict) else False,
        "kind": getattr(spec, "kind", ""),
        "output_type": getattr(spec, "output_type", ""),
        "compile_status": getattr(spec, "compile_status", ""),
        "failure_reason": getattr(spec, "failure_reason", ""),
        "supported_periods": list(getattr(spec, "supported_periods") or []),
        "dependencies": list(getattr(spec, "dependencies") or []),
    }


def resolve_rules_for_snapshot(
    cfg: AStockConfig, rule_ids: Optional[Sequence[str]] = None
) -> List[Tuple[str, str]]:
    """快照使用的规则集：显式列表或全部可执行规则（含预置两条）。"""
    if rule_ids:
        return [(str(r), "") for r in rule_ids]
    return [(rid, "") for rid in list_screenable_rule_ids(cfg)]


# ---------------------------------------------------------------------------
# 2. 快照组装与发布
# ---------------------------------------------------------------------------


def build_snapshot_payload(
    cfg: AStockConfig,
    summary: Dict[str, Any],
    *,
    rule_ids: Sequence[str],
    run_kind: str = "weekly_chain",
    rules_scope: str = sc.RULES_SCOPE_ALL,
    data_version: Optional[Dict[str, Any]] = None,
    snapshot_id: Optional[str] = None,
    universe_codes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """把 run_weekly_review 的 summary 转成契约快照结构。

    契约要点（§0/§1）：
    - universe_codes 完整清单（子集缓存判定 picked 越界的基础）；
      优先显式传入，否则从 summary.universe_codes 取，最后回源
      _resolve_codes（与计算同源，绝不 DEMO 兜底）；
    - failed_codes 从"全规则共享"改为逐规则集合；
    - no_data_codes 完整清单（不只数量）；
    - 逐规则 status：有失败票 → partial；规则级异常视为 error。
    - 快照 close 仅作展示；跟踪计算入场价必须重读（契约 §4）。

    ``rules_scope``（2026-09-16）：all=全量规则；subset=仅 rule_ids 这
    几条（「指定规则补算」）。子集快照的 universe_codes / no_data 等仍是
    全市场口径（筛选成本与规则数近似线性、与股票池无关），仅规则表是子集。
    """
    if run_kind not in sc.RUN_KINDS:
        raise ValueError(f"unknown run_kind: {run_kind!r}")
    if rules_scope not in sc.RULES_SCOPES:
        raise ValueError(f"unknown rules_scope: {rules_scope!r}")
    asof = int(summary.get("asof") or 0)
    if universe_codes is not None:
        uni_list: List[str] = [str(c) for c in universe_codes]
    else:
        uni_list = [str(c) for c in (summary.get("universe_codes") or [])]
        if not uni_list:
            from .indicator_review import _resolve_codes

            uni_list = list(_resolve_codes(cfg, None))
    no_data_codes = [
        str(c) for c in (summary.get("no_data_codes") or [])
    ]
    # 逐规则失败集合：计算层 errors 里带 (code, rule) 对
    rule_failed: Dict[str, set] = {str(r): set() for r in rule_ids}
    for e in summary.get("errors") or []:
        rid = str(e.get("rule") or "")
        code = str(e.get("code") or "")
        if rid and rid != "*" and rid in rule_failed and code:
            rule_failed[rid].add(code)
        elif rid == "*":
            for k in rule_failed:
                rule_failed[k].add(code)  # 加载级失败影响所有规则

    rules: List[Dict[str, Any]] = []
    for r in summary.get("rules", []):
        rid = str(r.get("rule_id"))
        failed = sorted(rule_failed.get(rid, set()))
        status = sc.RULE_STATUS_ERROR if not r.get("matched") and failed and len(failed) >= max(1, len(uni_list)) else (
            sc.RULE_STATUS_PARTIAL if failed else sc.RULE_STATUS_OK
        )
        rules.append(
            {
                "rule_id": rid,
                "sheet": r.get("sheet", ""),
                "status": status,
                "count": int(r.get("count") or 0),
                "matched": [
                    {"code": str(m.get("code")), "close": m.get("close")}
                    for m in r.get("matched") or []
                ],
                "failed_codes": failed,
            }
        )
    universe_fp = str(summary.get("universe_fingerprint") or "")
    rule_fps = dict(summary.get("rule_fingerprints") or {})
    dv = data_version or {}
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": snapshot_id or sc.new_snapshot_id(asof),
        "run_kind": run_kind,
        # 规则范围 + 实际覆盖的规则 ID（子集补算的读取方据此如实标注）
        "rules_scope": rules_scope,
        "scoped_rule_ids": (
            [str(r) for r in rule_ids] if rules_scope == sc.RULES_SCOPE_SUBSET else []
        ),
        "week_id": asof,  # 信号日=周内最后交易日由调用方保证（周五链语义）
        "asof": asof,
        "generated_at": summary.get("generated_at"),
        "status": str(summary.get("status") or ""),
        "universe_size": int(summary.get("universe_size") or len(uni_list)),
        "universe_codes": uni_list,
        "universe_fingerprint": universe_fp,
        "name_snapshot_id": str(summary.get("name_snapshot_id") or ""),
        "rule_fingerprints": rule_fps,
        "data_version": dv,
        "content_fingerprint": sc.content_fingerprint(
            rule_fps, universe_fp, str(summary.get("name_snapshot_id") or ""), dv, asof
        ),
        "scanned": int(summary.get("scanned") or 0),
        "missing_count": int(summary.get("missing_count") or 0),
        "no_data_codes": no_data_codes,
        "rules": rules,
        "duration_sec": summary.get("duration_sec"),
    }


def write_and_publish_snapshot(
    cfg: AStockConfig,
    payload: Dict[str, Any],
    *,
    source: str = "auto",
) -> Dict[str, Any]:
    """发布顺序（契约 §0）：O_EXCL 建快照 → 门槛 → 索引指针。

    失败任何一步都不留半发布状态：门槛不过 → 快照保留用于诊断，
    指针不动（该周视为未发布，启动补偿可重跑）。

    质量门槛不过时**返回正常结果而非抛异常**（审查后修正）：真实数据上
    no_data 比例超阈值（如大面积停牌）会让周五链 review 段崩栈——正确
    语义是"保留快照供诊断、本周不发布"，链尾照常走完并如实上报 verdict。
    """
    snapshot_id = str(payload["snapshot_id"])
    path = sc.snapshot_path(Path(cfg.storage_root), snapshot_id)
    sc.create_snapshot_file_exclusive(path, payload)

    run_kind = str(payload.get("run_kind"))
    week_id = int(payload.get("week_id") or 0)
    # 门槛先判：不过 → 保留诊断快照、指针不动（不进入 publish_snapshot 的
    # raise 路径，避免把"质量不达标"变成"链路事故"）
    verdict = sc.PublishPolicy().evaluate(payload)
    if not verdict["publishable"]:
        logger.warning(
            "screen_snapshots 快照未过发布门槛（%s），保留诊断不发布: %s",
            verdict["verdict"], path,
        )
        return {
            "published": False,
            "reason": f"quality_gate:{verdict['verdict']}",
            "verdict": verdict,
            "snapshot_id": snapshot_id,
            "snapshot_path": str(path),
        }
    decision = sc.publish_decision(
        Path(cfg.storage_root), week_id, snapshot_id, run_kind,
        rules_scope=sc.snapshot_rules_scope(payload),
    )
    if not decision["publish"]:
        return {
            "published": False,
            "reason": decision["reason"],
            "verdict": verdict,
            "snapshot_id": snapshot_id,
            "snapshot_path": str(path),
            "rules_scope": sc.snapshot_rules_scope(payload),
        }
    entry = sc.publish_snapshot(
        Path(cfg.storage_root), week_id, snapshot_id, run_kind, source=source
    )
    return {
        "published": True,
        "reason": decision["reason"],
        "verdict": verdict,
        "snapshot_id": snapshot_id,
        "snapshot_path": str(path),
        "rules_scope": sc.snapshot_rules_scope(payload),
        "week_index_entry": entry,
    }


def load_snapshot(
    cfg: AStockConfig, snapshot_id: str
) -> Optional[Dict[str, Any]]:
    p = sc.snapshot_path(Path(cfg.storage_root), snapshot_id)
    if not p.exists():
        return None
    import json

    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_published_snapshot_for_week(
    cfg: AStockConfig, week_id: int
) -> Optional[Dict[str, Any]]:
    """读该周正式发布快照（统计只认 published 指针，契约 §0）。"""
    idx = sc.load_week_index(Path(cfg.storage_root))
    entry = (idx.get("weeks") or {}).get(str(int(week_id)))
    if not entry or not entry.get("published_snapshot_id"):
        return None
    return load_snapshot(cfg, str(entry["published_snapshot_id"]))


def latest_published_snapshot(cfg: AStockConfig) -> Optional[Dict[str, Any]]:
    """最近一周的发布快照（前端 latest 自动加载的数据源）。"""
    idx = sc.load_week_index(Path(cfg.storage_root))
    weeks = idx.get("weeks") or {}
    if not weeks:
        return None
    latest_week = max(int(k) for k in weeks.keys())
    return load_published_snapshot_for_week(cfg, latest_week)


# ---------------------------------------------------------------------------
# 3. cache-first：快照服务筛选请求（契约 §规则组②）
# ---------------------------------------------------------------------------


def snapshot_covers(
    snap: Dict[str, Any],
    rule_ids: Sequence[str],
    *,
    current_rule_fps: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Any]:
    """快照能否完整服务本组规则（逐规则指纹+状态校验，非整包相等）。

    返回 {covered: [可用规则], uncovered: [缺失/失败/指纹不符规则],
    stale: [指纹已变化的规则]}。快照里 status=error 的规则不算覆盖
    （缺规则/规则失败不能当完整空结果）。

    fail-closed（审查 🔴B3）：current_rule_fps 中某规则的指纹为 None
    （规则已删除/解析失败）→ 一律判 stale——已改公式或已删除的规则
    绝不用旧快照结果冒充当前筛选。
    """
    snap_rules = {str(r.get("rule_id")): r for r in snap.get("rules") or []}
    snap_fps = dict(snap.get("rule_fingerprints") or {})
    covered: List[str] = []
    uncovered: List[str] = []
    stale: List[str] = []
    for rid in rule_ids:
        r = snap_rules.get(str(rid))
        if r is None:
            uncovered.append(str(rid))
            continue
        if str(r.get("status")) == sc.RULE_STATUS_ERROR:
            uncovered.append(str(rid))
            continue
        if current_rule_fps is not None:
            cur = current_rule_fps.get(str(rid))
            if cur is None or snap_fps.get(str(rid)) != cur:
                stale.append(str(rid))  # 指纹不可验证/已变/规则已删 → 陈旧
                continue
        covered.append(str(rid))
    return {"covered": covered, "uncovered": uncovered, "stale": stale}


def _ticket_status_for_code(
    snap: Dict[str, Any], rule_rec: Dict[str, Any], code: str
) -> str:
    """单票对单规则的评估状态（契约 §1 逐票五态，剔除 not_in_universe）。"""
    if code in {str(m.get("code")) for m in rule_rec.get("matched") or []}:
        return sc.TICKET_HIT
    if code in set(rule_rec.get("failed_codes") or []):
        return sc.TICKET_ERROR
    if code in set(snap.get("no_data_codes") or []):
        return sc.TICKET_NO_DATA
    universe = set(snap.get("universe_codes") or [])
    if code not in universe:
        return sc.TICKET_NOT_IN_UNIVERSE
    return sc.TICKET_MISS


def combine_snapshot_hits(
    snap: Dict[str, Any],
    *,
    rule_ids: Sequence[str],
    match_mode: str,
    codes: Optional[Sequence[str]] = None,
    rule_sheet_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """从快照组合出筛选结果（any/all 严格语义）。

    any（并集）：任一规则 hit 即入选；某票在某规则 error/no_data 时
    不宣称 miss——完整性标记进 incomplete/indeterminate。
    all（交集）：必须**所有请求规则都完成评估且都命中**才入选；任一
    规则 error/no_data → indeterminate（禁用剩余规则交集冒充完整结果）。
    picked 范围越界票 → not_in_universe（不静默 miss，逐票如实标注）。

    返回 dict：hits / indeterminate[{code, reasons{rule: status}}] /
    not_in_universe / rules / complete 标记 / 完整性计数。
    """
    if match_mode not in ("any", "all"):
        raise ValueError(f"match_mode 必须是 any/all，收到 {match_mode!r}")
    rid_list = [str(r) for r in rule_ids]
    snap_rules = {str(r.get("rule_id")): r for r in snap.get("rules") or []}
    sheet_map = rule_sheet_map or {
        rid: (snap_rules.get(rid, {}).get("sheet") or rid) for rid in rid_list
    }
    universe = [str(c) for c in (snap.get("universe_codes") or [])]
    scope = [str(c) for c in codes] if codes is not None else universe
    scope_set = set(scope)

    hits_by_code: Dict[str, Dict[str, Any]] = {}
    indeterminate: List[Dict[str, Any]] = []
    not_in_universe: List[str] = []
    incomplete_count = 0

    for code in universe:
        if scope_set and code not in scope_set:
            continue
        statuses: Dict[str, str] = {}
        for rid in rid_list:
            r = snap_rules.get(rid)
            statuses[rid] = (
                _ticket_status_for_code(snap, r, code) if r is not None else sc.TICKET_ERROR
            )
        problem = [rid for rid, s in statuses.items()
                   if s in (sc.TICKET_ERROR, sc.TICKET_NO_DATA)]
        if match_mode == "any":
            if any(s == sc.TICKET_HIT for s in statuses.values()):
                entry = hits_by_code.setdefault(
                    code, {"code": code, "close": None, "hit_rules": []}
                )
                for rid, s in statuses.items():
                    if s == sc.TICKET_HIT:
                        entry["hit_rules"].append(sheet_map.get(rid, rid))
                        m = next(
                            (m for m in snap_rules[rid].get("matched") or []
                             if str(m.get("code")) == code), None
                        )
                        if m and entry.get("close") is None:
                            entry["close"] = m.get("close")
            elif problem:
                incomplete_count += 1
                indeterminate.append(
                    {"code": code, "reasons": statuses,
                     "reason_kind": "eval_incomplete"}
                )
        else:  # all
            if problem:
                incomplete_count += 1
                indeterminate.append(
                    {"code": code, "reasons": statuses,
                     "reason_kind": "eval_incomplete"}
                )
            elif all(s == sc.TICKET_HIT for s in statuses.values()):
                entry = hits_by_code.setdefault(
                    code, {"code": code, "close": None, "hit_rules": []}
                )
                for rid, s in statuses.items():
                    if s == sc.TICKET_HIT:
                        entry["hit_rules"].append(sheet_map.get(rid, rid))
                        m = next(
                            (m for m in snap_rules[rid].get("matched") or []
                             if str(m.get("code")) == code), None
                        )
                        if m and entry.get("close") is None:
                            entry["close"] = m.get("close")

    for code in scope:
        if code not in set(universe):
            not_in_universe.append(code)

    hits = sorted(hits_by_code.values(), key=lambda e: e["code"])
    for e in hits:
        e["signal_date"] = int(snap.get("asof") or 0)
    return {
        "hits": hits,
        "indeterminate": indeterminate,
        "not_in_universe": not_in_universe,
        "incomplete_count": incomplete_count,
        "rules": [
            {
                "rule_id": rid,
                "sheet": sheet_map.get(rid, rid),
                "count": int(snap_rules.get(rid, {}).get("count") or 0),
                "status": str(snap_rules.get(rid, {}).get("status") or sc.RULE_STATUS_OK),
            }
            for rid in rid_list
        ],
        "complete": (incomplete_count == 0 and not not_in_universe),
        "asof": int(snap.get("asof") or 0),
        "universe_size": int(snap.get("universe_size") or 0),
    }
