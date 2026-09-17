# -*- coding: utf-8 -*-
"""跟踪查询只读路由（阶段 3 前置件）：三级钻取 UI 的数据源。

L0 指标总览  GET /api/v1/bagua/track/rules
L1 单指标周列表 GET /api/v1/bagua/track/rules/{rule_id}/weeks
L2 周明细    GET /api/v1/bagua/track/weeks/{entry_asof}

只读快照与跟踪产物；不做任何计算（计算在周五链/CLI/补偿）。
统计口径见 docs/plans/auto-screen-track/contract.md §3：
- 只认 published 指针的快照 + 当前 revision 的 track 产物；
- 空仓周胜率 null（非 0）；样本不足如实标注；
- 跨周聚合默认每周等权，票次加权另给；
- 近 N 周 = 最近 N 个信号自然周（周历序，非有收益的周）。
"""

from __future__ import annotations

import re
from pathlib import Path
from time import perf_counter
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from .context import ApiContext, get_ctx

router = APIRouter()

# 样本量低于该值的规则胜率标注「样本不足」（展示阈值，非统计结论）
MIN_SAMPLE_WARN = 30

# 契约 §9 回填提示全文：唯一文案来源。UI（本文件 L2 接口）与导出服务
# （service/track_export.py）都引用本常量，避免两处措辞漂移。
BACKFILL_NOTICE = (
    "本数据为按当前规则、当前可用股票池及历史行情重建，可能存在股票池、"
    "历史名称/ST 信息和数据修订偏差；不代表当时实际发布名单。"
)

# rule_id 长度上限（防超长入参打爆导出文件名/meta；与 bagua 导出勾选口径一致）
MAX_RULE_ID_LEN = 128

# 导出下载文件名白名单：仅允许 bagua_track_ 前缀 + ASCII 安全字符 + .xlsx。
# 严格白名单（而非黑名单）从源头否定路径分隔符、盘符、URL 编码穿越等一切
# 非预期输入；再配合 resolve() 落在导出目录内的二次校验防符号链接逃逸。
_TRACK_EXPORT_NAME_RE = re.compile(r"^bagua_track_[A-Za-z0-9_.-]+\.xlsx$")


def _load_json(path: Path):
    import json

    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _ms_since(t0: float) -> float:
    """毫秒计时（0.1ms 精度）：接口分段计时的统一取整处。"""
    return round((perf_counter() - t0) * 1000.0, 1)


def _published_weeks(cfg) -> dict:
    """发布索引 {week_id_str: entry}（只认指针，契约 §0）。"""
    from ..service import screen_contract as sc

    idx = sc.load_week_index(Path(cfg.storage_root))
    return idx.get("weeks") or {}


def _track_for_week(cfg, snap: dict, entry_asof: int) -> Optional[dict]:
    """该周当前采用的跟踪产物（current 指针；无则 None=待结算）。"""
    from ..service import screen_contract as sc

    snap_id = str(snap.get("snapshot_id") or "")
    cur = _load_json(sc.track_current_path(Path(cfg.storage_root), snap_id))
    if not cur or not cur.get("tracking_revision_id"):
        return None
    return _load_json(
        sc.track_path(
            Path(cfg.storage_root), snap_id, str(cur["tracking_revision_id"])
        )
    )


def _mean(values) -> Optional[float]:
    vals = [v for v in (values or []) if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 6)


def _with_exec_excess_stats(
    track: Optional[dict], rule_id: str, agg: Optional[dict]
) -> Optional[dict]:
    """派生并注入 exec 超额口径统计（契约 §3，不 bump immutable track schema）。

    UI 主显示的超额必须统一为首个实际交易日开盘口径（excess_exec）；
    历史产物已在逐票 rows[] 中落盘 excess_exec，在此只读层派生聚合字段，
    避免仅为展示字段重算并覆盖不可变历史产物。
    """
    if agg is None:
        return None
    new_agg = dict(agg)
    if not track or not track.get("rows"):
        new_agg.setdefault("excess_exec_valid_count", 0)
        new_agg.setdefault("mean_excess_exec", None)
        new_agg.setdefault("win_rate_excess_exec", None)
        return new_agg

    rid_str = str(rule_id)
    # 筛选属于该规则、评估状态正常、可成交、且具有有效 exec 超额的样本
    # 严格排除 limit_up_unbuyable / no_bar / unknown / benchmark 缺失 (excess_exec=None)
    excess_rows = [
        r
        for r in track.get("rows") or []
        if str(r.get("rule_id")) == rid_str
        and r.get("status", "ok") == "ok"
        and r.get("fill_status", "ok") == "ok"
        and r.get("excess_exec") is not None
    ]
    cnt = len(excess_rows)
    new_agg["excess_exec_valid_count"] = cnt
    new_agg["mean_excess_exec"] = _mean([r["excess_exec"] for r in excess_rows])
    if cnt > 0:
        pos_cnt = sum(1 for r in excess_rows if float(r["excess_exec"]) > 0)
        new_agg["win_rate_excess_exec"] = round(pos_cnt / cnt, 4)
    else:
        new_agg["win_rate_excess_exec"] = None
    return new_agg


# 规则目录 TTL 缓存秒数：list_screen_rules 会扫指标目录（重）；跟踪页刷新/
# 搜索很频繁，目录却在秒级内不会变
_CATALOG_TTL_SEC = 30


def _current_rule_catalog(ctx) -> Dict[str, dict]:
    """当前规则目录（规则中心可见 ∪ 周五链预置复核规则）：id -> 规则信息。

    2026-09-15 用户要求「跟踪里的指标与规则中心保持一致」：跟踪 L0/L1 原本
    只从历史快照取 rule_id 并集，导致 (a) 规则中心删掉的规则仍显示、
    (b) 同一公式的两个 rule_id（tn6_ 与其配对源 txt_）重复成两条。

    目录本体在 service/screening.current_rule_catalog（与跟踪导出共用同一
    实现，保证页面与导出同口径）；本函数只做 30s TTL 缓存包装——跟踪页刷新
    /搜索很频繁，目录却在秒级内不会变。删除规则后要立即在页面消失最多有
    30s 缓存延迟（扫指标目录的代价换来的，可接受）。

    降级：目录取不到（规则库异常/CI 无公式）时返回空 dict，调用方按
    「不过滤、不去重」处理——宁可多显示历史，也不能让整页空白。
    """
    import time as _time

    cache = getattr(ctx, "track_catalog_cache", None)
    if cache is None:  # 兼容未装配该字段的旧 context（测试直接构造时）
        cache = {"ts": 0.0, "data": None}
        try:
            ctx.track_catalog_cache = cache
        except Exception:  # noqa: BLE001 — 不可写则每次现算
            cache = None
    if cache is not None and cache["data"] is not None:
        if _time.time() - float(cache["ts"]) < _CATALOG_TTL_SEC:
            return cache["data"]
    try:
        from ..service.screening import current_rule_catalog

        out = current_rule_catalog(ctx.cfg)
    except Exception:  # noqa: BLE001 — 目录不可用不阻塞只读路由（fail-open）
        return {}
    if cache is not None:
        cache["ts"] = _time.time()
        cache["data"] = out
    return out


def _pick_canonical_rule_id(ids: List[str], catalog: Dict[str, dict]) -> str:
    """同指纹的多个 rule_id 里选一个代表身份（确定性，避免每次刷新换行）。

    打分与导出共用 service/screening.pick_canonical_rule_id 的同一实现——
    两处若各自写一份，将来改一处忘另一处，页面与导出会选出不同代表。
    """
    from ..service.screening import pick_canonical_rule_id

    return pick_canonical_rule_id(ids, catalog)


def _merge_by_fingerprint(
    segments: Dict[tuple, dict], catalog: Dict[str, dict]
) -> List[dict]:
    """把同指纹的 (rule_id, fp) 段合并成一行（同公式只展示一条）。

    合并规则：同一周若多个 id 都有数据，取 canonical 那条（同公式重复计入
    会双计票次）；canonical 缺该周则用兄弟 id 的周数据补位（历史周可能只有
    其中一个 id 有快照）。无指纹的老快照段按 rule_id 独立，不参与归并。
    """
    groups: Dict[str, List[tuple]] = {}
    for (rid, fp), seg in segments.items():
        key = str(fp) if fp else "__no_fp__:" + str(rid)
        groups.setdefault(key, []).append((str(rid), seg))
    out: List[dict] = []
    for key, items in groups.items():
        canonical = _pick_canonical_rule_id([rid for rid, _ in items], catalog)
        by_week: Dict[int, dict] = {}
        # canonical 先入，保证同周优先取它的数据
        for rid, seg in sorted(items, key=lambda x: (x[0] != canonical, x[0])):
            for w in seg.get("weeks") or []:
                wid = int(w["week_id"])
                if wid not in by_week:
                    by_week[wid] = w
        merged = {
            "rule_id": canonical,
            "fingerprint": "" if key.startswith("__no_fp__:") else key,
            "weeks": [by_week[k] for k in sorted(by_week)],
            # 同公式被归并掉的其它 id（规则中心里可能还在）：供排查与提示
            "merged_rule_ids": sorted(
                rid for rid, _ in items if rid != canonical
            ),
        }
        out.append(merged)
    return out


def _rule_name_of(rule_id: str, catalog: Dict[str, dict]) -> str:
    """规则中心当前名称（目录缺失时留空，由前端回落 rule_id）。"""
    info = catalog.get(str(rule_id)) if catalog else None
    return str((info or {}).get("name") or "")


@router.get("/api/v1/bagua/track/rules")
def api_track_rules(
    weeks: int = Query(12, ge=1, le=104, description="汇总窗口（最近 N 个信号自然周）"),
    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    """L0 指标总览表：一行一个规则（按 rule_id + 指纹分段）。

    聚合默认每周等权（该口径 valid>0 的周才计入，显示有效周数）；
    票次加权另列。样本 < 30 标 insufficient_sample。
    """
    from ..service import screen_contract as sc

    # 当前规则目录：用于删除同步过滤 + 名称来源（用户要求与规则中心一致）
    catalog = _current_rule_catalog(ctx)
    week_map = _published_weeks(ctx.cfg)
    week_ids = sorted(int(k) for k in week_map.keys())[-weeks:] if week_map else []

    # rule_id+指纹 → 周序列（规则改公式后历史段隔离，契约 §3）
    segments: dict = {}
    for wid in week_ids:
        from ..service.screen_snapshots import load_published_snapshot_for_week

        snap = load_published_snapshot_for_week(ctx.cfg, wid)
        if snap is None:
            continue
        track = _track_for_week(ctx.cfg, snap, wid)
        snap_fps = snap.get("rule_fingerprints") or {}
        for r in snap.get("rules") or []:
            rid = str(r.get("rule_id"))
            # 删除同步：规则中心已删除（user 硬删）/归档/隐藏的规则不再展示。
            # 目录为空（规则库异常/CI 无公式目录）时 filter 停用——宁可多显示
            # 历史数据，也不能因目录故障让整页空白（fail-open）。
            if catalog and rid not in catalog:
                continue
            fp = str(snap_fps.get(rid) or "")
            seg = segments.setdefault(
                (rid, fp),
                {"rule_id": rid, "fingerprint": fp, "weeks": []},
            )
            agg = None
            if track:
                raw_agg = next(
                    (a for a in track.get("rule_aggregates") or []
                     if str(a.get("rule_id")) == rid),
                    None,
                )
                agg = _with_exec_excess_stats(track, rid, raw_agg)
            seg["weeks"].append(
                {
                    "week_id": wid,
                    "asof": snap.get("asof"),
                    "run_kind": snap.get("run_kind"),
                    # 规则范围：subset = 「指定规则补算」周（该周只有这几条
                    # 规则有名单与跟踪结果，前端标签/提示据此渲染）
                    "rules_scope": sc.snapshot_rules_scope(snap),
                    "scoped_rule_ids": sc.scoped_rule_ids(snap),
                    "selected_count": agg.get("selected_count") if agg else int(r.get("count") or 0),
                    "settled": bool(track and track.get("completion") == sc.TRACK_COMPLETE),
                    "completion": (track or {}).get("completion") if track else "no_product",
                    "aggregate": agg,
                }
            )

    # 同指纹多 id 归并成一行（同公式只展示一条；canonical 优先当前规则目录
    # 里可执行的那条），并按目录做删除同步过滤。
    out = []
    for seg in sorted(
        _merge_by_fingerprint(segments, catalog),
        key=lambda s: (s["rule_id"], s["fingerprint"]),
    ):
        rid, fp = seg["rule_id"], seg["fingerprint"]
        settled_weeks = [w for w in seg["weeks"] if w["settled"]]
        # 每周等权：每 settled 周取该周（等权）平均收益
        weekly_sig = [
            w["aggregate"].get("mean_ret_close_sig")
            for w in settled_weeks
            if w["aggregate"] and w["aggregate"].get("mean_ret_close_sig") is not None
        ]
        weekly_exec = [
            w["aggregate"].get("mean_ret_close_exec")
            for w in settled_weeks
            if w["aggregate"] and w["aggregate"].get("mean_ret_close_exec") is not None
        ]
        weekly_excess = [
            w["aggregate"].get("mean_excess_sig")
            for w in settled_weeks
            if w["aggregate"] and w["aggregate"].get("mean_excess_sig") is not None
        ]
        weekly_excess_exec = [
            w["aggregate"].get("mean_excess_exec")
            for w in settled_weeks
            if w["aggregate"] and w["aggregate"].get("mean_excess_exec") is not None
        ]
        # 票次加权：全部周票池合并。
        # 胜率与收益是两套分子：胜率分子必须用各周 win_rate_* × 该周有效
        # 票数（= 票池合并后的逐票胜率），绝不能拿 mean_ret_* × 票数冒充
        # ——那算出来的是加权平均收益，会被当"胜率 1.5%"误读。
        total_selected = sum(w["selected_count"] or 0 for w in seg["weeks"])
        week_aggs = [w["aggregate"] for w in settled_weeks if w["aggregate"]]
        all_sig = [
            a["mean_ret_close_sig"] * int(a["valid_sig_count"])
            for a in week_aggs
            if a.get("mean_ret_close_sig") is not None and int(a.get("valid_sig_count") or 0) > 0
        ]
        all_exec = [
            a["mean_ret_close_exec"] * int(a["valid_exec_count"])
            for a in week_aggs
            if a.get("mean_ret_close_exec") is not None and int(a.get("valid_exec_count") or 0) > 0
        ]
        ticket_valid_sig = sum(
            int(a.get("valid_sig_count") or 0) for a in week_aggs
        )
        ticket_valid_exec = sum(
            int(a.get("valid_exec_count") or 0) for a in week_aggs
        )
        # 胜率分子/分母只计有胜率的周（真实产物里 win_rate 为 None ⟺ 该周
        # 无有效票，与 valid 计数天然一致；此处按字段严格判定，容忍合成产物）
        wr_sig_weeks = [a for a in week_aggs if a.get("win_rate_sig") is not None]
        wr_exec_weeks = [a for a in week_aggs if a.get("win_rate_exec") is not None]
        ticket_wr_sig_valid = sum(
            int(a.get("valid_sig_count") or 0) for a in wr_sig_weeks
        )
        ticket_wr_exec_valid = sum(
            int(a.get("valid_exec_count") or 0) for a in wr_exec_weeks
        )
        out.append(
            {
                "rule_id": rid,
                "fingerprint": fp,
                # 名称来自规则中心当前目录（后端直出，消除前端 wbs.rules 未加载
                # 时的时序坑：原来进跟踪页直接显示裸 rule_id）
                "rule_name": _rule_name_of(rid, catalog),
                # 同公式被归并掉的其它 rule_id（tn6_ 与其配对源 txt_ 等）
                "merged_rule_ids": seg.get("merged_rule_ids") or [],
                "tracked_weeks": len(seg["weeks"]),
                "settled_weeks": len(settled_weeks),
                # 其中来自「指定规则补算」（子集快照）的周数：前端在该规则行
                # 提示"含 N 周为指定规则补算"，避免用户以为这些周是全量周
                "subset_weeks": sum(
                    1 for w in seg["weeks"]
                    if str(w.get("rules_scope") or sc.RULES_SCOPE_ALL)
                    == sc.RULES_SCOPE_SUBSET
                ),
                "latest_week": seg["weeks"][-1]["week_id"] if seg["weeks"] else None,
                "latest_selected": seg["weeks"][-1]["selected_count"] if seg["weeks"] else None,
                "total_selected": total_selected,
                # 每周等权（默认口径）——胜率按各周逐票胜率再等权
                "weekly_equal_win_rate_sig": _mean(
                    [a.get("win_rate_sig") for a in week_aggs
                     if a.get("win_rate_sig") is not None]
                ),
                "weekly_equal_win_rate_exec": _mean(
                    [a.get("win_rate_exec") for a in week_aggs
                     if a.get("win_rate_exec") is not None]
                ),
                "weekly_equal_mean_ret_sig": _mean(weekly_sig),
                "weekly_equal_mean_ret_exec": _mean(weekly_exec),
                "weekly_equal_mean_excess_sig": _mean(weekly_excess),
                "weekly_equal_mean_excess_exec": _mean(weekly_excess_exec),
                "weekly_equal_valid_weeks_sig": len(weekly_sig),
                "weekly_equal_valid_weeks_exec": len(weekly_exec),
                "weekly_equal_valid_weeks_excess": len(weekly_excess),
                "weekly_equal_valid_weeks_excess_exec": len(weekly_excess_exec),
                # 近 5 周趋势：最多返回最近 5 个相关周（周历升序），直接供前端原生 SVG 绘制 Sparkline，杜绝 N+1
                "trend_weeks": [
                    {
                        "week_id": int(w["week_id"]),
                        "settled": bool(w["settled"]),
                        "mean_ret_exec": (
                            w["aggregate"].get("mean_ret_close_exec")
                            if w.get("aggregate")
                            else None
                        ),
                        "win_rate_exec": (
                            w["aggregate"].get("win_rate_exec")
                            if w.get("aggregate")
                            else None
                        ),
                        "mean_excess_exec": (
                            w["aggregate"].get("mean_excess_exec")
                            if w.get("aggregate")
                            else None
                        ),
                    }
                    for w in seg["weeks"][-5:]
                ],
                # 票次加权（并列口径）：逐票胜率 = Σ(周胜率×周有效数)/Σ有效数
                # （分母只计有胜率的周；与 mean_ret 的分母可能不同属正常）
                "ticket_win_rate_sig": (
                    round(
                        sum(
                            a["win_rate_sig"] * int(a.get("valid_sig_count") or 0)
                            for a in wr_sig_weeks
                        ) / ticket_wr_sig_valid,
                        6,
                    )
                    if ticket_wr_sig_valid > 0 else None
                ),
                "ticket_win_rate_exec": (
                    round(
                        sum(
                            a["win_rate_exec"] * int(a.get("valid_exec_count") or 0)
                            for a in wr_exec_weeks
                        ) / ticket_wr_exec_valid,
                        6,
                    )
                    if ticket_wr_exec_valid > 0 else None
                ),
                "ticket_mean_ret_sig": (
                    round(sum(all_sig) / ticket_valid_sig, 6)
                    if ticket_valid_sig > 0 else None
                ),
                "ticket_mean_ret_exec": (
                    round(sum(all_exec) / ticket_valid_exec, 6)
                    if ticket_valid_exec > 0 else None
                ),
                "ticket_valid_count_sig": ticket_valid_sig,
                "ticket_valid_count_exec": ticket_valid_exec,
                "insufficient_sample": total_selected < MIN_SAMPLE_WARN,
            }
        )
    return {"ok": True, "count": len(out), "rules": out, "window_weeks": len(week_ids)}


@router.get("/api/v1/bagua/track/rules/{rule_id}/weeks")
def api_track_rule_weeks(
    rule_id: str,
    weeks: int = Query(26, ge=1, le=104),
    fingerprint: Optional[str] = Query(
        None, description="L0 行的指纹（同公式归并组）；前端点击时带上"
    ),
    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    """L1 单指标历史周列表（倒序）。

    2026-09-15：接规则目录并把**同指纹的兄弟 id**（tn6_ 与其配对源 txt_、
    改名前后的 user_ 规则等）的周一起查、每周只取一份——L0 已按指纹归并成
    一行，若 L1 仍只认单一 rule_id，被归并掉的那些周会出现缺口。

    指纹来源优先级：请求参数 ``fingerprint``（前端从 L0 行带来，最可靠）>
    按入口 id 出现过的周反查。前者必须支持：新导入的 id（如 tn6_）可能在任何
    快照里都没出现过，而它的兄弟 id（txt_）有历史周——只靠反查会查不到。
    """
    from ..service import screen_contract as sc
    from ..service.screen_snapshots import load_published_snapshot_for_week

    catalog = _current_rule_catalog(ctx)
    week_map = _published_weeks(ctx.cfg)
    week_ids = sorted(int(k) for k in week_map.keys())[-weeks:] if week_map else []
    # 第一遍：缓存快照 + 确定指纹集合（请求参数优先；否则按入口 id 反查，
    # 入口规则改公式会换指纹，故是集合）
    snaps: dict = {}
    target_fps: set = set()
    if fingerprint:
        target_fps.add(str(fingerprint))
    for wid in week_ids:
        snap = load_published_snapshot_for_week(ctx.cfg, wid)
        if snap is None:
            continue
        snaps[wid] = snap
        if not fingerprint and any(
            str(r.get("rule_id")) == rule_id for r in snap.get("rules") or []
        ):
            fp = str((snap.get("rule_fingerprints") or {}).get(rule_id) or "")
            if fp:
                target_fps.add(fp)
    rows = []
    for wid in reversed(week_ids):
        snap = snaps.get(wid)
        if snap is None:
            continue
        fps = snap.get("rule_fingerprints") or {}
        recs = {str(r.get("rule_id")): r for r in snap.get("rules") or []}
        version_conflict = False
        if fingerprint:
            # 显式版本是硬约束：只认该指纹（与 L2 同一口径）。
            # 入口 id 存在但指纹不同 = 该周该规则是另一个版本（公式改过），
            # 不能拿它的数据冒充用户要看的那一版，如实标注冲突。
            want = str(fingerprint)
            rid_used = next(
                (k for k in recs if str(fps.get(k) or "") == want), None
            )
            version_conflict = rid_used is None and rule_id in recs
        elif rule_id in recs:
            rid_used = rule_id
        elif target_fps:
            rid_used = next(
                (k for k in recs if str(fps.get(k) or "") in target_fps), None
            )
        else:
            rid_used = None
        if rid_used is None:
            if version_conflict:
                # 冲突周也要出现在列表里（否则用户以为数据丢了），只是不带统计
                rows.append(
                    {
                        "week_id": wid,
                        "asof": snap.get("asof"),
                        "run_kind": snap.get("run_kind"),
                        "rules_scope": sc.snapshot_rules_scope(snap),
                        "scoped_rule_ids": sc.scoped_rule_ids(snap),
                        "rule_fingerprint": str(fps.get(rule_id) or ""),
                        "recorded_rule_id": rule_id,
                        "rule_status": recs.get(rule_id, {}).get("status"),
                        "selected_count": None,
                        "completion": None,
                        "aggregate": None,
                        "snapshot_id": snap.get("snapshot_id"),
                        "version_conflict": True,
                    }
                )
            continue  # 该周快照没算这条规则（如新规则上线前）
        rec = recs[rid_used]
        track = _track_for_week(ctx.cfg, snap, wid)
        agg = None
        if track:
            raw_agg = next(
                (a for a in track.get("rule_aggregates") or []
                 if str(a.get("rule_id")) == rid_used),
                None,
            )
            agg = _with_exec_excess_stats(track, rid_used, raw_agg)
        rows.append(
            {
                "week_id": wid,
                "asof": snap.get("asof"),
                "run_kind": snap.get("run_kind"),
                # subset = 该周为「指定规则补算」：前端在周行上标注，避免把
                # 部分名单当成那周的全部筛选结果
                "rules_scope": sc.snapshot_rules_scope(snap),
                "scoped_rule_ids": sc.scoped_rule_ids(snap),
                "rule_fingerprint": fps.get(rid_used),
                # 该周实际取数用的 rule_id（与入口不同即说明是兄弟 id 补位）
                "recorded_rule_id": rid_used,
                "rule_status": rec.get("status"),
                "selected_count": int(rec.get("count") or 0),
                "completion": (track or {}).get("completion") if track else "no_product",
                "aggregate": agg,
                "snapshot_id": snap.get("snapshot_id"),
                "version_conflict": False,
            }
        )
    resp = {
        "ok": True,
        "rule_id": rule_id,
        "rule_name": _rule_name_of(rule_id, catalog),
        "count": len(rows),
        "weeks": rows,
    }
    # 规则已在规则中心删除/归档：历史周仍如实返回（数据不可变），但打标记让
    # 前端提示「该规则已不在规则中心」，而不是让用户以为跟踪数据丢了
    if catalog and rule_id not in catalog:
        resp["removed_from_catalog"] = True
    return resp


def _mark_rows_bagua_pending(rows: List[dict]) -> None:
    """延后加载占位：卦象列留空并显式标记 pending。

    前端据此显示「加载中…」而不是「—」（无数据）或既有结论——未算出的卦象
    绝不能被渲染成已有判断（用户明确的展示边界）。
    """
    for r in rows:
        r["week_gua"] = ""
        r["bagua"] = None
        r["bagua_state"] = "pending"


def _enrich_rows_with_bagua(cfg, entry_asof: int, rows: List[dict]) -> None:
    """为每行股票注入周卦、月卦及高岛/倾向共识信息（与每周导出 Excel 同源口径）。

    2026-09-16 性能整改：改为调用 service.bagua_query.bagua_week_month_info
    （共享价格面 session 的周/月共享物化 + 版本化结果缓存）。原先此处不传
    session，快路径直接失效 → 每股把 WEEK/MONTH 各物化一次，739 行的一周实测
    26.7s 全部堵在这里。逐票包装仍在：同一请求内同票只算一次，跨请求靠服务层
    缓存（键含行情版本与知识库指纹）。

    失败/无数据/测试 mock 环境时静默回退，绝不阻断 L2 跟踪明细返回。
    """
    if not rows:
        return
    try:
        from ..service.bagua_query import (
            bagua_period_asof_map,
            bagua_week_month_info,
            get_bagua_calculator,
        )

        asof = int(entry_asof)
        asof_map = bagua_period_asof_map(asof)
        calc = get_bagua_calculator(cfg)
        per_code: dict = {}
        for r in rows:
            code = str(r.get("code") or "").strip()
            if not code:
                r["week_gua"] = ""
                r["bagua"] = None
                r["bagua_state"] = "empty"
                continue
            if code not in per_code:
                try:
                    per_code[code] = bagua_week_month_info(
                        cfg, code=code, asof=asof, asof_map=asof_map, calc=calc
                    )
                except Exception:  # noqa: BLE001 — 单票失败只标该票，不放倒整张表
                    per_code[code] = {
                        "week_gua": "",
                        "bagua": None,
                        "state": "error",
                        "month_state": "error",
                    }
            info = per_code[code]
            r["week_gua"] = info.get("week_gua") or ""
            r["bagua"] = info.get("bagua")
            r["bagua_state"] = str(info.get("state") or "empty")
            # 月卦单独记账：周卦成功但月卦失败时，前端只把月卦面板标成可重试
            r["bagua_month_state"] = str(info.get("month_state") or "empty")
    except Exception:
        for r in rows:
            r.setdefault("week_gua", "")
            r.setdefault("bagua", None)
            r.setdefault("bagua_state", "error")


def _short_code(code: str) -> str:
    """展示用短代码（去掉 SSE.STK./SZSE.STK. 前缀，只留 6 位码）。

    只影响展示字段，产物里的 code 保持完整 std_code（查卦象等接口要用）。
    """
    try:
        from ..forecast.name_norm import normalize_stock_code

        return normalize_stock_code(code) or str(code or "")
    except Exception:  # noqa: BLE001 — 规范化失败按原样展示
        return str(code or "")


# 历史快照反查指纹时的最大回溯周数（与 L1 默认窗口一致）：只在「本周快照没有
# 这个 rule_id」时走这条路，命中即停，正常情况不会扫这么多。
_VERSION_LOOKBACK_WEEKS = 26


def _version_fingerprint(
    ctx, snap: dict, entry_asof: int, rule_id: Optional[str], fingerprint: Optional[str]
) -> str:
    """本周该规则的身份指纹（**版本精确**，用于匹配同指纹的兄弟 id）。

    优先级（全部是历史证据，绝不用当前规则目录猜身份、也不做 tn6_/txt_ 前缀替换）：

    1. 请求参数 ``fingerprint``：前端从 L0 行 / L1 周行带来的**那一周的**指纹，
       最可靠（L0 的归并分组本身就来自历史快照的指纹）；
    2. 本周快照自己的 ``rule_fingerprints[rule_id]``；
    3. 回看历史快照找该 rule_id 最近出现时的指纹，且该指纹必须**在本周快照里
       存在**才采用——这样既能让 tn6_X 落到本周的兄弟 id（txt_X）上，又不会把
       该规则的其他版本（公式改过）拉进同一周。
    取不到 → 空串（调用方退化为「只按传入 id 过滤」，与旧行为一致）。
    """
    rid = str(rule_id or "").strip()
    fps_all = snap.get("rule_fingerprints") or {}
    if fingerprint is not None and str(fingerprint).strip():
        return str(fingerprint).strip()
    if not rid:
        return ""
    own = str(fps_all.get(rid) or "")
    if own:
        return own
    # 本周没有这个 id（如 L0 归并出的 canonical tn6_X，而该周只有 txt_X）：
    # 按历史快照反查最近指纹，且必须落在本周快照已记录的指纹集合里。
    present = {str(v or "") for v in fps_all.values()} - {""}
    if not present:
        return ""
    try:
        from ..service.screen_snapshots import load_published_snapshot_for_week

        week_map = _published_weeks(ctx.cfg)
        recent = sorted((int(k) for k in week_map.keys()), reverse=True)[
            :_VERSION_LOOKBACK_WEEKS
        ]
        for wid in recent:
            if int(wid) == int(entry_asof):
                continue
            old = load_published_snapshot_for_week(ctx.cfg, wid)
            if old is None:
                continue
            fp = str((old.get("rule_fingerprints") or {}).get(rid) or "")
            if fp and fp in present:
                return fp
    except Exception:  # noqa: BLE001 — 反查失败按「无指纹」处理（不扩展兄弟 id）
        return ""
    return ""


def _resolve_week_identity(
    ctx, snap: dict, entry_asof: int, rule_id: Optional[str], fingerprint: Optional[str]
) -> dict:
    """本周该规则的身份解析：``{group_ids, fingerprint, source}``。

    ``group_ids`` = 同版本（同指纹）的 rule_id 集合；``None`` = 不过滤（该周全部）。
    ``source`` 说明身份是怎么定下来的：

    - ``all``              未传 rule_id（看该周全部规则）；
    - ``param``            请求显式给了 fingerprint，且该指纹在本周快照里有记录；
    - ``param_unmatched``  **显式指纹在本周快照里没有记录 → 硬约束下无匹配**
                           （绝不回落到入口 id，否则会把别的版本混进来）；
    - ``snapshot``         本周快照自己记录了该 rule_id 的指纹；
    - ``history``          本周没有该 rule_id，按历史快照反查到本周存在的指纹；
    - ``entry_only``       指纹无从确定（老快照/全新规则），退化为只按入口 id。

    显式 ``fingerprint`` 是**硬约束**（用户 2026-09-16 复核）：入口 id 与指纹冲突时
    不再无条件保留入口 id；传不存在的指纹返回「无匹配」而不是入口 id 的名单。
    """
    fps_all = {str(k): str(v or "") for k, v in (snap.get("rule_fingerprints") or {}).items()}
    if rule_id is None:
        return {"group_ids": None, "fingerprint": "", "source": "all"}
    rid = str(rule_id)
    want = str(fingerprint or "").strip()
    if want:
        ids = {k for k, v in fps_all.items() if v == want}
        if not ids:
            return {"group_ids": set(), "fingerprint": want, "source": "param_unmatched"}
        return {"group_ids": ids, "fingerprint": want, "source": "param"}
    own = fps_all.get(rid) or ""
    if own:
        return {
            "group_ids": {k for k, v in fps_all.items() if v == own} | {rid},
            "fingerprint": own,
            "source": "snapshot",
        }
    hist = _version_fingerprint(ctx, snap, entry_asof, rid, None)
    if hist:
        return {
            "group_ids": {k for k, v in fps_all.items() if v == hist},
            "fingerprint": hist,
            "source": "history",
        }
    return {"group_ids": {rid}, "fingerprint": "", "source": "entry_only"}


def _select_representative_rules(
    ctx, snap: dict, fps_all: Dict[str, str], rows_all: List[dict],
    identity: dict,
) -> dict:
    """每周每个指纹组**只选一个代表规则**，再取该规则的名单与统计（与 L0 同口径）。

    背景（用户 2026-09-16 复核的反例）：同指纹 R 命中 A、S 命中 A,B 时，
    原实现把兄弟规则名单**并集**后再逐票去重 → L2 得到 2 只，而 L0 选代表规则 R
    只有 1 只。两者口径不等价，且并集是"默默"发生的。

    现在与 ``_merge_by_fingerprint`` 完全对齐：组内优先 canonical（
    ``pick_canonical_rule_id``，规则中心里可见/可执行者优先），canonical 本周没有
    数据时用兄弟补位（历史周可能只有其中一个 id 有产物）。组内多个 id 都有数据且
    数量不一致时，如实记录 ``divergence`` 暴露差异，不静默取并集。

    返回 ``{keep_rids, representative, divergence, group_counts}``。
    """
    row_cnt: Dict[str, int] = {}
    for r in rows_all:
        rid = str(r.get("rule_id") or "")
        row_cnt[rid] = row_cnt.get(rid, 0) + 1
    matched_cnt: Dict[str, int] = {}
    for r in snap.get("rules") or []:
        rid = str(r.get("rule_id") or "")
        matched_cnt[rid] = matched_cnt.get(rid, 0) + len(r.get("matched") or [])

    gid = identity.get("group_ids")
    if gid is None:
        # 不过滤：按指纹分组（无指纹的老快照按 id 独立成组）
        groups: Dict[str, set] = {}
        for rid in set(row_cnt) | set(matched_cnt):
            fp = fps_all.get(rid) or ""
            groups.setdefault(fp or ("__rid__:" + rid), set()).add(rid)
    else:
        groups = {identity.get("fingerprint") or "__group__": set(gid)}

    catalog = _current_rule_catalog(ctx)
    keep_rids: set = set()
    representative = ""
    divergence: Dict[str, dict] = {}
    for key, ids in groups.items():
        ids = sorted(set(ids))
        if not ids:
            continue  # 显式指纹在本周无匹配：这一组没有可展示的规则
        # 「有数据」：先看已结算产物行（L0 的口径也是看聚合有没有），
        # 全组都没有产物行时才退到快照命中（待结算）
        rows_here = [i for i in ids if row_cnt.get(i)]
        matched_here = [i for i in ids if matched_cnt.get(i)]
        rep = _pick_canonical_rule_id(ids, catalog)
        if rep not in rows_here:
            # canonical 本周没有产物（历史周可能只有兄弟 id 有）→ 用兄弟补位
            rep = rows_here[0] if rows_here else (
                rep if rep in matched_here
                else (matched_here[0] if matched_here else "")
            )
        if rep:
            keep_rids.add(rep)
            if key == (identity.get("fingerprint") or "__group__") and not representative:
                representative = rep
        with_data = sorted(set(rows_here) | set(matched_here))
        if len(with_data) > 1:
            counts = {
                i: {"rows": row_cnt.get(i, 0), "matched": matched_cnt.get(i, 0)}
                for i in with_data
            }
            base = counts.get(rep)
            if any(v != base for v in counts.values()):
                divergence[key] = {"representative": rep, "sibling_counts": counts}
    return {
        "keep_rids": keep_rids,
        # 只在「按规则过滤」时才有单一代表规则；未过滤视图是每组各一个代表，
        # 报 null 比随便挑一个更诚实
        "representative": representative
        or (sorted(keep_rids)[0] if (keep_rids and gid is not None) else ""),
        "divergence": divergence,
        "group_counts": {i: {"rows": row_cnt.get(i, 0), "matched": matched_cnt.get(i, 0)} for i in sorted(set(row_cnt) | set(matched_cnt))},
    }


def _dedupe_rows_by_identity(
    rows: List[dict], fps_all: Dict[str, str], entry_rule_id: Optional[str]
) -> List[dict]:
    """同 (代码, 指纹) 兜底去重：正常情况代表规则已经保证每组一条。

    保留策略（确定性，保证刷新不换行）：优先入口 rule_id，其次 id 字典序最小。
    """
    if not rows:
        return rows
    entry = str(entry_rule_id or "")
    best: Dict[tuple, dict] = {}
    order: List[tuple] = []
    for r in rows:
        rid = str(r.get("rule_id") or "")
        # 指纹取自快照的记录（产物行里没有该字段）；无指纹的老快照按 id 独立
        fp = str(fps_all.get(rid) or "")
        key = (str(r.get("code") or ""), fp or ("__rid__:" + rid))
        cur = best.get(key)
        if cur is None:
            best[key] = r
            order.append(key)
            continue
        cur_rid = str(cur.get("rule_id") or "")
        if rid == entry and cur_rid != entry:
            best[key] = r
        elif cur_rid != entry and rid < cur_rid:
            best[key] = r
    return [best[k] for k in order]


def _load_week_rows(
    ctx,
    entry_asof: int,
    rule_id: Optional[str],
    fingerprint: Optional[str] = None,
) -> dict:
    """读取该周明细所需的全部只读产物（L2 明细与批量卦象补齐共用同一实现）。

    共用是必须的：补齐接口若自己再筛一遍名单，前端拿到的卦象键就会与表格行
    对不上（名单口径漂移），而且会出现「列表有 83 只、补齐接口却拒绝它们」。
    规则身份（rule_id + 版本指纹）也在这一处解析，两个接口共用同一份名单。
    归属统计口径（发布快照指针 + 当前跟踪 revision）两处也天然一致。
    无发布快照 → 404（与明细接口同一错误语义）。

    返回 ``{"snap","track","rows","pending","timings_ms","identity","selection","fingerprint"}``；
    timings 记录各段读盘耗时（毫秒），供接口回传做线上分段计时；identity/selection
    说明规则身份是怎么定的、每组选了哪个代表规则（两个接口共用同一份结论）。
    """
    from ..service.screen_snapshots import load_published_snapshot_for_week

    timings: Dict[str, float] = {}
    t0 = perf_counter()
    snap = load_published_snapshot_for_week(ctx.cfg, int(entry_asof))
    timings["snapshot_ms"] = _ms_since(t0)
    if snap is None:
        raise HTTPException(404, f"该周无发布快照: {entry_asof}")
    t0 = perf_counter()
    track = _track_for_week(ctx.cfg, snap, int(entry_asof))
    timings["track_ms"] = _ms_since(t0)

    t0 = perf_counter()
    fps_all = {
        str(k): str(v or "") for k, v in (snap.get("rule_fingerprints") or {}).items()
    }
    identity = _resolve_week_identity(
        ctx, snap, int(entry_asof), rule_id, fingerprint
    )
    gid = identity["group_ids"]
    rows_all = [
        r
        for r in (track or {}).get("rows") or []
        if gid is None or str(r.get("rule_id")) in gid
    ]
    # 每组只保留一个代表规则（与 L0 的整周选择口径一致），再取该规则的名单
    selection = _select_representative_rules(
        ctx, snap, fps_all, rows_all, identity
    )
    keep_rids = selection["keep_rids"]
    timings["rule_filter_ms"] = _ms_since(t0)
    # 待结算票不来自产物（无 name 字段），读取时补名称：来源与结算层同一
    # 函数（stock_names），逐票解析有进程级缓存，量级=单周入选票数，成本可忽略。
    _name_cache: dict = {}

    def _name_of(c: str) -> str:
        if c not in _name_cache:
            try:
                # 同结算层：normalize_stock_code 统一取 6 位码（产物 code
                # 形态可能是 SSE.STK.600033 或 SZSE.000003.SZ）
                from ..forecast.name_norm import normalize_stock_code
                from ..service.stock_names import resolve_stock_name

                _name_cache[c] = str(
                    resolve_stock_name(
                        ctx.cfg, normalize_stock_code(c), std_code=str(c)
                    )
                    or ""
                )
            except Exception:  # noqa: BLE001 — 缺名如实留空，不影响接口
                _name_cache[c] = ""
        return _name_cache[c]

    t0 = perf_counter()
    # 浅拷贝加展示字段（daily 等大字段共享引用，不复制）
    rows = [
        dict(r, code_disp=_short_code(str(r.get("code") or "")))
        for r in rows_all
        if str(r.get("rule_id")) in keep_rids
    ]
    rows = _dedupe_rows_by_identity(rows, fps_all, selection["representative"])
    timings["rows_ms"] = _ms_since(t0)

    t0 = perf_counter()
    # 产物是不可变快照，但历史产物的 name 可能整列为空：结算发生在缺本地
    # 导入产物（无 TDX / 无 universe.json / 无周报快照）的部署上时名称源全缺，
    # name 被写成 ""。产物不回写（不可变），改为读取时按当前名称源补齐展示名；
    # 补不到仍留空，前端显示「—」，绝不拿代码冒充。导出层同一函数。
    from ..service.stock_names import fill_missing_names

    fill_missing_names(ctx.cfg, rows)
    timings["name_fill_ms"] = _ms_since(t0)

    # 快照里命中但 track 还没有的票 → 待结算清单（前端显示 pending 态）
    settled_codes = {str(r.get("code")) for r in rows}

    t0 = perf_counter()
    pending = []
    for r in snap.get("rules") or []:
        # 与 rows 同一份代表规则口径，否则归并后的 canonical 会漏票/多票
        if str(r.get("rule_id")) not in keep_rids:
            continue
        for m in r.get("matched") or []:
            c = str(m.get("code"))
            if c not in settled_codes:
                pending.append(
                    {
                        "code": c,
                        "code_disp": _short_code(c),
                        "name": _name_of(c),
                        "rule_id": r.get("rule_id"),
                        "close": m.get("close"),
                    }
                )
    timings["pending_ms"] = _ms_since(t0)
    pending = _dedupe_rows_by_identity(pending, fps_all, selection["representative"])
    return {
        "snap": snap,
        "track": track,
        "rows": rows,
        "pending": pending,
        "timings_ms": timings,
        "identity": identity,
        "selection": selection,
        "fingerprint": identity["fingerprint"],
    }


def _week_code_list(rows: List[dict], pending: List[dict]) -> List[str]:
    """该周全部标的（已结算 + 待结算，去重，稳定升序）。

    批量卦象补齐按此名单校验入参：前端只能就本周名单内的票请求补齐，
    不会把接口变成任意两三百只票的通用重算入口。
    """
    codes = {str(r.get("code") or "").strip() for r in rows}
    codes |= {str(p.get("code") or "").strip() for p in pending}
    codes.discard("")
    return sorted(codes)


@router.get("/api/v1/bagua/track/weeks/{entry_asof}")
def api_track_week_detail(
    entry_asof: int,
    rule_id: Optional[str] = Query(None, description="缺省=该周全部规则"),
    fingerprint: Optional[str] = Query(
        None,
        description="规则版本指纹（前端从 L0/L1 行带来）：与同指纹兄弟 id 一起匹配，缺省按快照/历史反查",
    ),
    bagua: str = Query(
        "defer",
        description="卦象加载方式：defer=先返回列表、卦象随后批量补齐（默认）；inline=同步算完再返回（旧行为）",
    ),
    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    """L2 周明细：该周入选的每只票一行（逐日表现全量）。

    2026-09-16 性能整改：默认 ``bagua=defer`` —— 名单、价格、收益、统计卡片
    不再等卦象计算（实测 739 行的一周里卦象占接口耗时 99.7%，26.7s）。此时行上
    的 ``week_gua`` 为空、``bagua`` 为 None、``bagua_state="pending"``，前端渲染
    「加载中…」，再由 ``/weeks/{asof}/bagua`` 分批补齐真实卦象。

    规则身份：``rule_id`` + ``fingerprint`` 共同决定本周名单（同指纹兄弟 id 一起
    命中、每组只取代表规则）。响应回带 ``fingerprint`` / ``rule_identity``，前端据此
    把同一身份传给批量补齐接口，保证列表与补齐范围一致。
    """
    bagua_mode = str(bagua or "").strip().lower()
    if bagua_mode not in ("defer", "inline"):
        raise HTTPException(400, "bagua 只支持 defer / inline")

    from ..service import screen_contract as sc

    t_start = perf_counter()
    loaded = _load_week_rows(ctx, entry_asof, rule_id, fingerprint)
    snap = loaded["snap"]
    track = loaded["track"]
    rows = loaded["rows"]
    matched_pending = loaded["pending"]
    timings: Dict[str, float] = dict(loaded["timings_ms"])

    # 周卦/月卦/高岛与共识倾向（V1.1.3 卦象共识增强）：
    # defer 只打占位标记；inline 同步算完（旧行为，保留给需要单请求拿全量的调用方）
    cache_stats = {"hit": 0, "miss": 0}
    t0 = perf_counter()
    if bagua_mode == "inline":
        from ..service.bagua_query import bagua_info_cache_stats

        before = bagua_info_cache_stats()
        _enrich_rows_with_bagua(ctx.cfg, int(entry_asof), rows)
        _enrich_rows_with_bagua(ctx.cfg, int(entry_asof), matched_pending)
        after = bagua_info_cache_stats()
        cache_stats = {
            "hit": max(0, after.get("hit", 0) - before.get("hit", 0)),
            "miss": max(0, after.get("miss", 0) - before.get("miss", 0)),
        }
    else:
        _mark_rows_bagua_pending(rows)
        _mark_rows_bagua_pending(matched_pending)
    timings["bagua_ms"] = _ms_since(t0)

    scope = sc.snapshot_rules_scope(snap)
    scope_ids = sc.scoped_rule_ids(snap)

    # UI 摘要指标（契约 §3，成交假设口径）：
    # 胜率与收益分母必须排除 limit_up_unbuyable / no_bar / unknown
    total_selected_codes = {str(r.get("code")) for r in rows} | {
        str(p.get("code")) for p in matched_pending
    }
    selected_count = len(total_selected_codes)

    valid_exec_rows = [
        r
        for r in rows
        if r.get("status", "ok") == "ok"
        and r.get("fill_status", "ok") == "ok"
        and r.get("ret_close_exec") is not None
    ]
    valid_exec_count = len(valid_exec_rows)
    if valid_exec_count > 0:
        pos_exec_count = sum(
            1 for r in valid_exec_rows if float(r["ret_close_exec"]) > 0
        )
        win_rate_exec = round(pos_exec_count / valid_exec_count, 4)
        mean_ret_exec = _mean([r["ret_close_exec"] for r in valid_exec_rows])
    else:
        win_rate_exec = None
        mean_ret_exec = None

    excess_exec_rows = [
        r for r in valid_exec_rows if r.get("excess_exec") is not None
    ]
    mean_excess_exec = (
        _mean([r["excess_exec"] for r in excess_exec_rows])
        if excess_exec_rows
        else None
    )

    if selected_count > 0:
        return_coverage_exec = round(valid_exec_count / selected_count, 4)
        excess_coverage_exec = round(len(excess_exec_rows) / selected_count, 4)
    else:
        return_coverage_exec = None
        excess_coverage_exec = None

    ui_summary = {
        "selected_count": selected_count,
        "valid_exec_count": valid_exec_count,
        "win_rate_exec": win_rate_exec,
        "mean_ret_exec": mean_ret_exec,
        "mean_excess_exec": mean_excess_exec,
        "return_coverage_exec": return_coverage_exec,
        "excess_coverage_exec": excess_coverage_exec,
    }

    return {
        "ok": True,
        "week_id": int(entry_asof),
        "asof": snap.get("asof"),
        "snapshot_id": snap.get("snapshot_id"),
        "run_kind": snap.get("run_kind"),
        "backfill": str(snap.get("run_kind")) == "backfill",
        # 规则范围（2026-09-16）：subset = 「指定规则补算」周。前端必须显示
        # scope_notice——该周只有这几条规则跑过筛选，其余规则当周无名单，
        # 不标注会被误读成"其他规则当周空仓"。
        "rules_scope": scope,
        "scoped_rule_ids": scope_ids,
        "scope_notice": (
            sc.subset_scope_notice(scope_ids) if scope == sc.RULES_SCOPE_SUBSET else None
        ),
        "completion": (track or {}).get("completion") if track else "no_product",
        "coverage": (track or {}).get("coverage"),
        "tracking_revision_id": (track or {}).get("tracking_revision_id"),
        # 跟踪周窗口（短周=实际交易日少于 5 天）：前端按真实交易日渲染逐日表头，
        # 不写死「周一~周五」（契约 §2 自然周窗口语义）
        "signal_week": (track or {}).get("signal_week"),
        "track_week": (track or {}).get("track_week"),
        "track_week_dates": (track or {}).get("track_week_dates") or [],
        "short_week": bool((track or {}).get("short_week")),
        "window_ended": (track or {}).get("window_ended"),
        "rows": rows,
        "pending_picks": matched_pending,
        "backfill_notice": BACKFILL_NOTICE if str(snap.get("run_kind")) == "backfill" else None,
        "ui_summary": ui_summary,
        # 卦象加载方式与本周标的数：defer 时前端据此按代码分批补齐（total 用于进度显示）
        "bagua_mode": bagua_mode,
        "bagua_total": len(total_selected_codes),
        # 规则身份回带：前端把同一个 rule_id + fingerprint 传给批量补齐接口，
        # 保证「列表有的票，补齐接口也认」
        "rule_id": rule_id,
        "fingerprint": loaded["fingerprint"],
        "rule_ids": sorted(loaded["selection"]["keep_rids"]),
        # 身份解析详情：source=param_unmatched 表示「该版本本周没有名单」，
        # sibling_divergence 表示同公式兄弟产物的名单/统计不一致（如实暴露，不静默取并集）
        "rule_identity": {
            "requested_rule_id": rule_id,
            "fingerprint": loaded["identity"]["fingerprint"],
            "source": loaded["identity"]["source"],
            "group_rule_ids": (
                sorted(loaded["identity"]["group_ids"])
                if loaded["identity"]["group_ids"] is not None
                else None
            ),
            "representative_rule_id": loaded["selection"]["representative"] or None,
            "kept_rule_ids": sorted(loaded["selection"]["keep_rids"]),
            "sibling_counts": loaded["selection"]["group_counts"],
            # 同公式兄弟产物的名单/统计不一致时列出（如实暴露，不静默取并集）
            "sibling_divergence": [
                {"fingerprint": key, **val}
                for key, val in sorted(loaded["selection"]["divergence"].items())
            ] or None,
        },
        # inline 时的卦象缓存命中数（defer 恒为 0）：判断「这次到底是算了还是复用」
        "bagua_cache": cache_stats,
        # 分段计时（毫秒）：线上排查「哪一段在等」用，不再靠猜
        "timings_ms": {**timings, "total_ms": _ms_since(t_start)},
    }


#: 批量补齐的代码数上限：单批过大会让一次请求变成分钟级重算，也会放大为
#: 「任意两三百只票的通用卦象重算入口」，故按周名单校验 + 单批封顶。
BAGUA_BATCH_MAX_CODES = 200


@router.get("/api/v1/bagua/track/weeks/{entry_asof}/bagua")
def api_track_week_bagua(
    entry_asof: int,
    codes: str = Query(..., description="逗号分隔的标的代码（本周名单内，最多 200 个）"),
    rule_id: Optional[str] = Query(None, description="与明细接口同口径的规则过滤"),
    fingerprint: Optional[str] = Query(
        None, description="规则版本指纹：必须与明细接口传同一个，否则名单会不一致"
    ),
    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    """L2 卦象批量补齐：给本周名单里的票补周卦/月卦/共识（列表已先行返回）。

    设计要点（2026-09-16 性能整改）：
    - 名单与明细接口**同一实现**（`_load_week_rows`）：rule_id + fingerprint 相同时
      名单必然一致，不会出现「列表有 83 只、补齐接口却拒绝它们」；
    - 只接受本周名单内的代码，其余进 ``skipped_codes`` 原样回报（不静默丢弃）；
    - 单批封顶 ``BAGUA_BATCH_MAX_CODES``，前端按展示顺序分批，切换周时丢弃旧批；
    - 每项带 ``state``（ok/empty/error）与 ``month_state``，前端区分
      「加载中/无数据/失败」，月卦失败时可单独重试；
    - 结果经版本化缓存复用（键含行情版本 + 知识库指纹），重复进入秒回。

    响应带回 ``snapshot_id`` / ``tracking_revision_id``：前端必须校验与当前页面
    加载的周一致后才合并，防止旧批数据覆盖新页面。
    """
    raw = str(codes or "").strip()
    wanted: List[str] = []
    seen = set()
    for part in raw.split(","):
        c = part.strip()
        if not c or c in seen:
            continue
        seen.add(c)
        wanted.append(c)
    if not wanted:
        raise HTTPException(400, "codes 不能为空")
    if len(wanted) > BAGUA_BATCH_MAX_CODES:
        raise HTTPException(
            400, f"单批最多 {BAGUA_BATCH_MAX_CODES} 个代码（当前 {len(wanted)}）"
        )

    from ..service.bagua_query import (
        bagua_info_cache_stats,
        bagua_period_asof_map,
        bagua_week_month_info,
        get_bagua_calculator,
    )

    t_start = perf_counter()
    loaded = _load_week_rows(ctx, entry_asof, rule_id, fingerprint)
    timings: Dict[str, float] = dict(loaded["timings_ms"])
    week_codes = _week_code_list(loaded["rows"], loaded["pending"])
    allowed = set(week_codes)
    accepted = [c for c in wanted if c in allowed]
    skipped = [c for c in wanted if c not in allowed]

    t0 = perf_counter()
    asof = int(entry_asof)
    asof_map = bagua_period_asof_map(asof)
    calc = get_bagua_calculator(ctx.cfg)
    before = bagua_info_cache_stats()
    items = []
    for c in accepted:
        try:
            info = bagua_week_month_info(
                ctx.cfg, code=c, asof=asof, asof_map=asof_map, calc=calc
            )
        except Exception as e:  # noqa: BLE001 — 单票失败只标该票，整批继续
            items.append(
                {
                    "code": c,
                    "week_gua": "",
                    "bagua": None,
                    "state": "error",
                    "month_state": "error",
                    "adjust": "",
                    "error_reason": str(e)[:200],
                }
            )
            continue
        items.append(
            {
                "code": c,
                "week_gua": info.get("week_gua") or "",
                "bagua": info.get("bagua"),
                "state": str(info.get("state") or "empty"),
                # 月卦状态单独回传：前端据此把月卦面板标失败并纳入重试范围
                "month_state": str(info.get("month_state") or "empty"),
                "adjust": info.get("adjust") or "",
            }
        )
    timings["bagua_ms"] = _ms_since(t0)
    after = bagua_info_cache_stats()

    return {
        "ok": True,
        "week_id": asof,
        "snapshot_id": loaded["snap"].get("snapshot_id"),
        "tracking_revision_id": (loaded["track"] or {}).get("tracking_revision_id"),
        "rule_id": rule_id,
        "fingerprint": loaded.get("fingerprint") or "",
        "week_code_total": len(week_codes),
        "requested": len(wanted),
        "count": len(items),
        "items": items,
        "skipped_codes": skipped,
        "cache": {
            "hit": max(0, after.get("hit", 0) - before.get("hit", 0)),
            "miss": max(0, after.get("miss", 0) - before.get("miss", 0)),
        },
        "timings_ms": {**timings, "total_ms": _ms_since(t_start)},
    }


@router.get("/api/v1/bagua/track/export")
def api_track_export(
    weeks: int = Query(12, description="导出窗口（最近 N 个发布信号自然周）"),
    rule_id: Optional[str] = Query(None, description="只导出该规则；缺省=全部"),
    entry_asof: Optional[str] = Query(
        None, description="8 位信号日（YYYYMMDD），限定单周导出"
    ),
    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    """把已结算的跟踪产物导出为 xlsx（指标汇总 / 周汇总 / 周明细 + meta）。

    参数边界显式返回 400（不依赖 Query 的 ge/le，避免 FastAPI 默认 422）；
    无发布周时返回 HTTP 200 + {"ok": false, "reason": "no_published_week"}，
    前端据此提示而不是当成错误。
    """
    if weeks < 1 or weeks > 104:
        raise HTTPException(400, "weeks 必须在 1..104 之间")
    rid: Optional[str] = None
    if rule_id is not None:
        rid = rule_id.strip()
        if len(rid) > MAX_RULE_ID_LEN:
            raise HTTPException(400, f"rule_id 超长（上限 {MAX_RULE_ID_LEN}）")
        rid = rid or None
    eaf: Optional[int] = None
    if entry_asof is not None and str(entry_asof).strip() != "":
        s = str(entry_asof).strip()
        if not re.fullmatch(r"\d{8}", s):
            raise HTTPException(400, "entry_asof 必须为 8 位数字（YYYYMMDD）")
        eaf = int(s)

    from ..service.track_export import export_tracking_xlsx

    return export_tracking_xlsx(ctx.cfg, weeks=weeks, rule_id=rid, entry_asof=eaf)


@router.get("/api/v1/bagua/track/export/download")
def api_track_export_download(
    file: str = Query(..., description="导出文件名（服务端返回的 file 字段）"),
    ctx: ApiContext = Depends(get_ctx),
) -> FileResponse:
    """下载跟踪导出文件。

    安全：文件名严格白名单（仅 bagua_track_*.xlsx 的 ASCII 安全字符），
    否定一切路径分隔符/盘符/编码穿越；再 resolve() 并确认解析后的父目录
    就是导出根目录，防符号链接指向目录外。任一不满足 → 400；文件不存在 → 404。
    """
    from ..service.track_export import EXPORT_SUBDIRNAME, XLSX_MEDIA_TYPE

    name = str(file or "")
    if not _TRACK_EXPORT_NAME_RE.fullmatch(name):
        raise HTTPException(400, "非法文件名")
    root = (Path(ctx.cfg.storage_root) / EXPORT_SUBDIRNAME).resolve()
    candidate = (root / name).resolve()
    if candidate.parent != root:
        raise HTTPException(400, "非法文件路径")
    if not candidate.is_file():
        raise HTTPException(404, f"导出文件不存在: {name}")
    return FileResponse(candidate, media_type=XLSX_MEDIA_TYPE, filename=name)
