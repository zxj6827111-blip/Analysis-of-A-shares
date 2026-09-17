# -*- coding: utf-8 -*-
"""基准：L2 周明细首屏与周卦补齐的分段计时（2026-09-16 性能整改验收工具）。

整改前：周明细接口在返回前逐票现算周卦+月卦，实测 739 行的一周 26.7s 中
99.7% 花在这里——名单/价格/收益/统计卡片全都被挡在卦象后面。

整改后：接口默认 ``bagua=defer`` 先返回列表（行上只留 ``bagua_state=pending``
占位），周卦由 ``/weeks/{asof}/bagua`` 分批补齐，结果按「行情版本 + 知识库
指纹」缓存。

本工具通过**真实 HTTP**打运行中的服务（不导入仓库代码），因此可直接在
服务器上跑，用来回答「哪一段在等、线上占比多少」：

  1. defer 首屏耗时 + 响应体积 + 服务端分段计时（timings_ms）
  2. 周卦分批补齐：逐批耗时、首批耗时（用户看到周卦的时间）、总计
  3. 重复补齐（全缓存）耗时
  4. inline（旧行为，单请求拿全量）耗时
  5. 内部一致性：批量补齐 vs inline —— **注意两者调用同一个新实现**，只能
     证明「接口之间一致」，不能证明「与改造前等价」

「与改造前等价」由 ``--legacy-check`` 单独核对：该模式在**仓库内**运行改造前
的逐票实现（``_legacy_week_month_info``，逻辑逐行取自 commit 802f8a0 的
``api_routes/tracking.py::_enrich_rows_with_bagua``），与批量接口的结果逐票
比对周卦与月卦文本。因此它需要在有仓库与行情根的机器上跑（本机/服务器目录内），
不能像纯 HTTP 模式那样对任意地址使用。

用法：

    python tools/bench_track_l2_timing.py --week 20260731
    python tools/bench_track_l2_timing.py --week 20260911 --rule tn6_735金叉及趋势
    python tools/bench_track_l2_timing.py --week 20260731 --legacy-check 60
    python tools/bench_track_l2_timing.py --week 20260731 --inline-only   # 冷态基线

参数：

    --week      信号周（YYYYMMDD），必填
    --rule      只看某条规则（与页面 L1 入口同口径）
    --chunk     补齐分批大小（默认 60，与前端 WBT_BAGUA_CHUNK 对齐）
    --base-url  服务地址（默认 http://127.0.0.1:8080）
    --fingerprint  规则版本指纹（canonical id 在本周快照缺失时必须带上）
    --no-check  跳过内部一致性核对（大批量时省时间）
    --inline-only  只测旧行为 inline（服务刚重启、缓存为空时跑才是冷态基线）
    --legacy-check N  取前 N 只票，与改造前的逐票实现逐票比对周卦/月卦

注意：首次补齐包含价格面 session 构建与知识库加载（冷启动），后续批次明显
更快；缓存命中与否会体现在 ``cache.hit/miss`` 与各批耗时上。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def _get(base: str, path: str, query: dict) -> tuple[dict, float, int]:
    """GET + 计时；返回 (json, 秒, 响应字节数)。"""
    url = base.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=1800) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {e.code} {url}\n{body[:500]}") from e
    dt = time.perf_counter() - t0
    return json.loads(raw.decode("utf-8")), dt, len(raw)


def _fmt_timings(t: dict) -> str:
    if not t:
        return "-"
    order = ("snapshot_ms", "track_ms", "rows_ms", "pending_ms", "bagua_ms", "total_ms")
    return " ".join(f"{k.replace('_ms', '')}={t.get(k)}" for k in order if k in t)


def _legacy_week_month_info(cfg, code, asof, asof_map):
    """改造前的逐票实现（等价性核对的基准，逐行取自 commit 802f8a0）。

    特征就是当时的问题本身：**不传 session**（多周期共享物化快路径失效，
    WEEK/MONTH 各物化一次），且不区分周/月状态。这里保持原样，只用来和
    新实现比对结果文本，不要用它算性能。
    """
    from wtpy.apps.astock.service.bagua_query import (
        BaguaCalculator,
        _bagua_combo,
        _bagua_consensus_label,
        _bagua_gaodao_explain,
        _bagua_yao_explain,
        _query_bagua_periods_for_code,
    )

    calc = BaguaCalculator.from_json(cfg.bagua_json) if cfg.bagua_json else None
    info = {"week_gua": "", "bagua": None}
    for adj in ("tushare_qfq", "raw"):
        try:
            res = _query_bagua_periods_for_code(
                cfg, code=code, asof=asof, periods=["WEEK", "MONTH"],
                adjust=adj, calc=calc, asof_map=asof_map,
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
        except Exception:  # noqa: BLE001 — 与旧实现一致：换下一口径
            continue
    return info


def _norm_bagua(info):
    """比对用的归一化视图：只比较文本事实，容忍「月卦面板空 dict vs None」这类
    表示差异（新实现用 None 表示该周期没算出来，旧实现写的是空字符串结构）。"""
    fields = ("combo", "yao_explain", "gaodao", "consensus", "action_signal")

    def part(d):
        if not d:
            return None
        out = {k: str(d.get(k) or "") for k in fields}
        return out if any(out.values()) else None

    b = (info or {}).get("bagua") or {}
    return {
        "week_gua": str((info or {}).get("week_gua") or ""),
        "week": part(b.get("week")),
        "month": part(b.get("month")),
    }


def _legacy_check(week: str, codes: list[str], merged: dict, limit: int) -> int:
    """与改造前逐票实现逐票比对（需在仓库内运行，可读到行情根）。"""
    import os
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    try:
        from wtpy.apps.astock.config import get_default_config
        from wtpy.apps.astock.service.bagua_query import bagua_period_asof_map
    except Exception as e:  # noqa: BLE001
        print(f"[legacy] 无法导入仓库模块（{e}）——该模式需在仓库内运行")
        return 2
    cfg = get_default_config()
    print(f"[legacy] 行情根 = {cfg.market_data_root}（存在: {cfg.market_data_root.exists()}）")
    asof = int(week)
    asof_map = bagua_period_asof_map(asof)
    sample = codes[:limit]
    diff = []
    for c in sample:
        new = _norm_bagua(merged.get(c) or {})
        old = _norm_bagua(_legacy_week_month_info(cfg, c, asof, asof_map))
        if new != old:
            diff.append((c, new.get("week_gua"), old.get("week_gua"),
                         new.get("month") and new["month"].get("combo"),
                         old.get("month") and old["month"].get("combo")))
    print(f"[legacy] 与改造前实现比对 {len(sample)} 只：差异 {len(diff)} 处")
    for d in diff[:5]:
        print(f"    ⚠ {d[0]}  周卦 新={d[1]!r} 旧={d[2]!r} | 月卦 新={d[3]!r} 旧={d[4]!r}")
    return 1 if diff else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="L2 周明细首屏/周卦补齐分段计时")
    ap.add_argument("--week", required=True, help="信号周 YYYYMMDD")
    ap.add_argument("--rule", default=None, help="只测某条规则")
    ap.add_argument("--chunk", type=int, default=60, help="补齐分批大小（默认 60）")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080", help="服务地址")
    ap.add_argument("--fingerprint", default=None, help="规则版本指纹（canonical id 用）")
    ap.add_argument("--no-check", action="store_true", help="跳过内部一致性核对")
    ap.add_argument(
        "--inline-only", action="store_true",
        help="只测旧行为 inline（在服务刚重启、缓存为空时跑才是冷态基线）",
    )
    ap.add_argument(
        "--legacy-check", type=int, default=0, metavar="N",
        help="取前 N 只票与改造前的逐票实现逐票比对（需在仓库内运行）",
    )
    args = ap.parse_args(argv)

    week = args.week
    q = {"rule_id": args.rule} if args.rule else {}
    if args.fingerprint:
        q["fingerprint"] = args.fingerprint
    print(f"目标: {args.base_url}  周={week}" + (f"  规则={args.rule}" if args.rule else ""))

    if args.inline_only:
        qi = dict(q)
        qi["bagua"] = "inline"
        ji, dti, nbi = _get(args.base_url, f"/api/v1/bagua/track/weeks/{week}", qi)
        rows_i = ji.get("rows") or []
        print(f"\n[inline 冷态基线] {dti:.2f}s  响应={nbi / 1024:.0f}KB  行={len(rows_i)}"
              f" pending={len(ji.get('pending_picks') or [])}")
        print(f"    服务端分段: {_fmt_timings(ji.get('timings_ms'))} cache={ji.get('bagua_cache')}")
        return 0

    # ---- 1. defer 首屏 ----
    detail, dt, nbytes = _get(args.base_url, f"/api/v1/bagua/track/weeks/{week}", q)
    rows = detail.get("rows") or []
    pend = detail.get("pending_picks") or []
    print(f"\n[1] defer 首屏      {dt:.3f}s  响应={nbytes / 1024:.0f}KB  行={len(rows)} 待结算={len(pend)}")
    print(f"    bagua_mode={detail.get('bagua_mode')} bagua_total={detail.get('bagua_total')}")
    print(f"    服务端分段: {_fmt_timings(detail.get('timings_ms'))}")
    if str(detail.get("bagua_mode")) != "defer":
        print("    ⚠ 该服务仍是旧版接口（无 defer）或显式 inline，首屏仍含卦象计算")
    detail2, dt2, _ = _get(args.base_url, f"/api/v1/bagua/track/weeks/{week}", q)
    print(f"    二次首屏          {dt2:.3f}s（读盘/session 已热）")

    # ---- 2. 分批补齐 ----
    codes: list[str] = []
    seen = set()
    for r in rows + pend:
        c = str(r.get("code") or "")
        if c and c not in seen:
            seen.add(c)
            codes.append(c)
    if not codes:
        print("\n[2] 无标的可补齐（该周无名单）")
        return 0

    mk = dict(q)
    chunks = [codes[i:i + args.chunk] for i in range(0, len(codes), args.chunk)]
    merged: dict[str, dict] = {}
    t_all = time.perf_counter()
    first = None
    worst = (0.0, 0)
    for i, part in enumerate(chunks, 1):
        bj, dtc, _ = _get(
            args.base_url, f"/api/v1/bagua/track/weeks/{week}/bagua",
            {**mk, "codes": ",".join(part)},
        )
        if first is None:
            first = dtc
        if dtc > worst[0]:
            worst = (dtc, i)
        for it in bj.get("items") or []:
            merged[str(it.get("code"))] = it
        print(f"    批{i:>3}: {len(part):>4}只 {dtc:6.2f}s "
              f"cache={bj.get('cache')} | {_fmt_timings(bj.get('timings_ms'))}")
    total = time.perf_counter() - t_all
    print(f"[2] 周卦分批补齐    {total:.2f}s  {len(codes)}只 / {len(chunks)}批  "
          f"首批={first:.2f}s（用户看到前排周卦的时间）  最慢批={worst[0]:.2f}s(第{worst[1]}批)")
    states: dict[str, int] = {}
    for it in merged.values():
        states[str(it.get("state"))] = states.get(str(it.get("state")), 0) + 1
    print(f"    状态分布: {states}")

    # ---- 3. 重复补齐（缓存） ----
    bj, dtr, _ = _get(
        args.base_url, f"/api/v1/bagua/track/weeks/{week}/bagua",
        {**mk, "codes": ",".join(codes[:args.chunk])},
    )
    print(f"[3] 重复补齐(第1批) {dtr:.3f}s cache={bj.get('cache')}（命中缓存应接近 0）")

    # ---- 4. inline 旧行为 ----
    q_inline = dict(q)
    q_inline["bagua"] = "inline"
    ji, dti, nbi = _get(args.base_url, f"/api/v1/bagua/track/weeks/{week}", q_inline)
    ti = ji.get("timings_ms") or {}
    print(f"[4] inline 旧行为   {dti:.2f}s  响应={nbi / 1024:.0f}KB | "
          f"{_fmt_timings(ti)} cache={ji.get('bagua_cache')}")
    if (ji.get("bagua_cache") or {}).get("hit"):
        print("    注：本次 inline 有缓存命中（先前补齐已算过），冷态参考请在服务重启后"
              "用 --inline-only 只测 inline")
    else:
        print(f"    → 同一周：旧行为首屏 {dti:.2f}s（卦象 {ti.get('bagua_ms')}ms）"
              f" vs 新首屏 {dt:.3f}s；卦象改为分批补齐，首批 {first:.2f}s")

    # ---- 5. 内部一致性核对（同一实现的接口间）----
    rc = 0
    if not args.no_check:
        diff = []
        for r in ji.get("rows") or []:
            c = str(r.get("code") or "")
            it = merged.get(c)
            if it is None:
                continue
            if (r.get("week_gua") or "") != (it.get("week_gua") or "") or r.get("bagua") != it.get("bagua"):
                diff.append(c)
        print(f"[5] 内部一致性      批量补齐 vs inline 差异 {len(diff)}/{len(merged)}")
        print("    注：两者调用同一个新实现（且共享缓存），只证明接口间一致；"
              "「与改造前等价」看 [6]")
        if diff:
            print(f"    ⚠ 差异样例: {diff[:5]}")

    # ---- 6. 与改造前逐票实现等价核对（在仓库内跑）----
    if args.legacy_check > 0:
        rc = _legacy_check(week, codes, merged, args.legacy_check)
    return rc


if __name__ == "__main__":
    sys.exit(main())
