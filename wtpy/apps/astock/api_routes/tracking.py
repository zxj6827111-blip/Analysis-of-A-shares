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
from typing import Optional

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
        if rule_id in recs:
            rid_used = rule_id
        elif target_fps:
            rid_used = next(
                (k for k in recs if str(fps.get(k) or "") in target_fps), None
            )
        else:
            rid_used = None
        if rid_used is None:
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


def _enrich_rows_with_bagua(cfg, entry_asof: int, rows: List[dict]) -> None:
    """为每行股票注入周卦、月卦及高岛/倾向共识信息（与每周导出 Excel 同源口径）。

    失败/无数据/测试 mock 环境时静默回退，绝不阻断 L2 跟踪明细返回。
    """
    if not rows:
        return
    try:
        from ..service.bagua_query import (
            BaguaCalculator,
            _bagua_combo,
            _bagua_consensus_label,
            _bagua_gaodao_explain,
            _bagua_yao_explain,
            _month_attributions,
            _query_bagua_periods_for_code,
        )

        calc = (
            BaguaCalculator.from_json(cfg.bagua_json)
            if getattr(cfg, "bagua_json", None)
            else None
        )
        asof = int(entry_asof)
        attrs = _month_attributions(asof)
        month_asof = attrs[0]["cast_asof"] if attrs else asof
        asof_map = {"WEEK": asof, "MONTH": month_asof}

        bagua_cache: Dict[str, dict] = {}
        for r in rows:
            code = str(r.get("code") or "").strip()
            if not code:
                r["week_gua"] = ""
                r["bagua"] = None
                continue

            if code not in bagua_cache:
                info: dict = {"week_gua": "", "bagua": None}
                for adj in ("tushare_qfq", "raw"):
                    try:
                        res = _query_bagua_periods_for_code(
                            cfg,
                            code=code,
                            asof=asof,
                            periods=["WEEK", "MONTH"],
                            adjust=adj,
                            calc=calc,
                            asof_map=asof_map,
                        )
                        w = res.get("WEEK")
                        if w and w.get("ok"):
                            m = res.get("MONTH")
                            week_combo = _bagua_combo(w)
                            month_combo = _bagua_combo(m) if m else ""
                            info["week_gua"] = week_combo
                            info["bagua"] = {
                                "week": {
                                    "combo": week_combo,
                                    "yao_explain": _bagua_yao_explain(w),
                                    "gaodao": _bagua_gaodao_explain(w),
                                    "consensus": _bagua_consensus_label(w) or "一般",
                                    "action_signal": str((w.get("bagua") or {}).get("action_signal") or ""),
                                },
                                "month": {
                                    "combo": month_combo,
                                    "yao_explain": _bagua_yao_explain(m) if m else "",
                                    "gaodao": _bagua_gaodao_explain(m) if m else "",
                                    "consensus": (_bagua_consensus_label(m) or "一般") if m else "一般",
                                    "action_signal": str((m.get("bagua") or {}).get("action_signal") or "") if m else "",
                                },
                            }
                            break
                    except Exception:
                        continue
                bagua_cache[code] = info

            cached = bagua_cache[code]
            r["week_gua"] = cached.get("week_gua") or ""
            r["bagua"] = cached.get("bagua")
    except Exception:
        for r in rows:
            r.setdefault("week_gua", "")
            r.setdefault("bagua", None)


@router.get("/api/v1/bagua/track/weeks/{entry_asof}")
def api_track_week_detail(
    entry_asof: int,
    rule_id: Optional[str] = Query(None, description="缺省=该周全部规则"),
    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    """L2 周明细：该周入选的每只票一行（逐日表现全量）。"""
    from ..service import screen_contract as sc
    from ..service.screen_snapshots import load_published_snapshot_for_week

    snap = load_published_snapshot_for_week(ctx.cfg, int(entry_asof))
    if snap is None:
        raise HTTPException(404, f"该周无发布快照: {entry_asof}")
    track = _track_for_week(ctx.cfg, snap, int(entry_asof))
    rows_all = (track or {}).get("rows") or []

    # 展示用短代码（用户要求：去掉 SSE.STK./SZSE.STK. 前缀，只留 6 位码）。
    # 只改展示字段，产物里的 code 保持完整 std_code（查卦象等接口要用）。
    def _short(code: str) -> str:
        try:
            from ..forecast.name_norm import normalize_stock_code

            return normalize_stock_code(code) or str(code or "")
        except Exception:  # noqa: BLE001 — 规范化失败按原样展示
            return str(code or "")

    # rule_id 过滤按「同指纹组」匹配：L0 已把同公式的多个 id 归并成 canonical，
    # 传下来的 canonical 可能与产物行里的原始 id 不同（tn6_X vs txt_X），
    # 只匹配单个 id 会查不到任何行。
    rid_filter: Optional[set] = None
    if rule_id is not None:
        rid_filter = {str(rule_id)}
        fps_all = snap.get("rule_fingerprints") or {}
        fp = str(fps_all.get(str(rule_id)) or "")
        if fp:
            rid_filter |= {
                str(k) for k, v in fps_all.items() if str(v or "") == fp
            }
    # 浅拷贝加展示字段（daily 等大字段共享引用，不复制）
    rows = [
        dict(r, code_disp=_short(str(r.get("code") or "")))
        for r in rows_all
        if rid_filter is None or str(r.get("rule_id")) in rid_filter
    ]
    # 快照里命中但 track 还没有的票 → 待结算清单（前端显示 pending 态）
    settled_codes = {str(r.get("code")) for r in rows}
    # 待结算票不来自产物（无 name 字段），读取时补名称：来源与结算层同一
    # 函数（stock_names），逐票解析有全局缓存，量级=单周入选票数，成本可忽略。
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

    matched_pending = []
    for r in snap.get("rules") or []:
        # 同指纹组匹配（与 rows 过滤同口径），否则归并后的 canonical id 会漏票
        if rid_filter is not None and str(r.get("rule_id")) not in rid_filter:
            continue
        for m in r.get("matched") or []:
            c = str(m.get("code"))
            if c not in settled_codes:
                matched_pending.append(
                    {
                        "code": c,
                        "code_disp": _short(c),
                        "name": _name_of(c),
                        "rule_id": r.get("rule_id"),
                        "close": m.get("close"),
                    }
                )

    # 注入周卦、月卦、高岛与共识倾向字段（V1.1.3 卦象共识增强）
    _enrich_rows_with_bagua(ctx.cfg, int(entry_asof), rows)
    _enrich_rows_with_bagua(ctx.cfg, int(entry_asof), matched_pending)

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
