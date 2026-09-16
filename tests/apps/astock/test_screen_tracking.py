# -*- coding: utf-8 -*-
"""阶段 2 跟踪层测试：screen_tracking（双口径收益/成交性/版本化/补偿）。

全部合成数据——bar_loader 注入合成 K 线 + 合成日历 + 手写发布快照，
绝不依赖 E 盘 AStockData 真实数据（仿 test_signal_returns.py 的注入模式）。

时间轴约定（两个自然周）：
    信号周 2025-01-06(周一) ~ 2025-01-10(周五)，信号日=20250110
    跟踪周 2025-01-13(周一) ~ 2025-01-17(周五)，week_end=20250117
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.service import screen_contract as sc
from wtpy.apps.astock.service import screen_snapshots as ss
from wtpy.apps.astock.service import screen_tracking as st


# 日期：信号周（周一 0106 ~ 周五 0110）+ 跟踪周（周一 0113 ~ 周五 0117）
SIGNAL_WEEK = [20250106, 20250107, 20250108, 20250109, 20250110]
TRACK_WEEK = [20250113, 20250114, 20250115, 20250116, 20250117]
SIGNAL_DATE = SIGNAL_WEEK[-1]   # 周五链语义：信号日=周内最后交易日
WEEK_END = TRACK_WEEK[-1]
ALL_DATES = SIGNAL_WEEK + TRACK_WEEK

BENCH = st.BENCHMARK_CODE  # SSE.IDX.000300
C1 = "SZSE.STK.000001"
C2 = "SZSE.STK.000002"
C3 = "SZSE.STK.000003"

DV1 = {"delta_commit_seq": 7, "base_dataset_id": "ds_a"}
DV2 = {"delta_commit_seq": 8, "base_dataset_id": "ds_a"}


@pytest.fixture()
def cfg(tmp_path: Path, monkeypatch) -> AStockConfig:
    c = AStockConfig()
    # 全部产物写 tmp：绝不让 track/state 落进仓库真实 storage。
    # market_data_root 是 env 驱动的只读 property：设 MARKET_DATA_ROOT 隔离
    #（conftest 的 autouse fixture 会在用例后回滚环境）。
    c.storage_root = tmp_path / "storage"
    md_root = tmp_path / "md"
    md_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MARKET_DATA_ROOT", str(md_root))
    return c


def _calendar(extra_after: list | None = None) -> TradeCalendar:
    """合成日历：跟踪周之后默认还有交易日（窗口已结束）。"""
    dates = list(ALL_DATES) + list(extra_after or [20250120])
    return TradeCalendar(dates)


def _bars(
    closes: dict[int, float],
    *,
    opens: dict[int, float] | None = None,
    highs: dict[int, float] | None = None,
    lows: dict[int, float] | None = None,
    only: list[int] | None = None,
):
    """合成单票数组 dict（trade_date/open/high/low/close numpy 列）。"""
    dates = sorted(closes) if only is None else list(only)
    o = opens or {}
    h = highs or {}
    lo = lows or {}
    return {
        "trade_date": np.array(dates, dtype="int64"),
        "open": np.array([o.get(d, closes[d]) for d in dates], dtype=float),
        "high": np.array([h.get(d, closes[d]) for d in dates], dtype=float),
        "low": np.array([lo.get(d, closes[d]) for d in dates], dtype=float),
        "close": np.array([closes[d] for d in dates], dtype=float),
    }


def _flat(closes_by_date: dict[int, float]):
    """无波动合成（open=high=low=close）。"""
    return _bars(closes_by_date)


def _loader(arrays: dict, data_version: dict | None = None):
    """注入式 PlaneBatchLoader：忽略窗口参数返回固定 arrays。"""
    def _load(codes):
        out = {c: arrays[c] for c in codes if c in arrays}
        meta = {}
        if data_version is not None:
            meta["data_version"] = dict(data_version)
        return out, meta
    return _load


def _track(
    cfg,
    *,
    qfq: dict,
    bench_arrays: dict | None = None,
    raw: dict | None = None,
    calendar=None,
    **kw,
):
    """合成调用便捷入口：默认恒等复权（raw 面 = qfq 面，即复权因子=1，
    未复权价与复权价一致的合成世界——涨停判定用 qfq 同一组价即可）。"""
    arrays = dict(qfq)
    if bench_arrays is not None:
        arrays[BENCH] = bench_arrays.get(BENCH) or bench_arrays
    return st.compute_weekly_tracking(
        cfg, SIGNAL_DATE,
        bar_loader=_loader(arrays),
        raw_loader=_loader(raw if raw is not None else arrays),
        benchmark_loader=_loader(bench_arrays or {}),
        calendar=calendar if calendar is not None else _calendar(),
        **kw,
    )


def _publish_snapshot(
    cfg: AStockConfig,
    *,
    matched: list[dict] | None = None,
    week_id: int = SIGNAL_DATE,
) -> dict:
    """手写最小契约快照并发布（published 指针 = track 的唯一入口）。"""
    matched = matched if matched is not None else [
        {"code": C1, "close": 999.0},   # 快照 close 故意离谱：入场价必须重读
        {"code": C2, "close": 999.0},
    ]
    snap = {
        "schema_version": "1",
        "snapshot_id": f"snap_{week_id}_test",
        "run_kind": "weekly_chain",
        "week_id": week_id,
        "asof": week_id,
        "generated_at": "2025-01-10 18:00:00",
        "status": "ok",
        "universe_size": 3,
        "universe_codes": [C1, C2, C3],
        "universe_fingerprint": "ufp",
        "name_snapshot_id": "ns",
        "rule_fingerprints": {"r1": "fp_r1"},
        "data_version": {},
        "content_fingerprint": "cf",
        "scanned": 3,
        "missing_count": 0,
        "no_data_codes": [],
        "rules": [
            {
                "rule_id": "r1", "sheet": "规则一", "status": "ok",
                "count": len(matched), "matched": matched, "failed_codes": [],
            }
        ],
        "duration_sec": 0.1,
    }
    path = sc.snapshot_path(Path(cfg.storage_root), snap["snapshot_id"])
    sc.create_snapshot_file_exclusive(path, snap)
    sc.publish_snapshot(
        Path(cfg.storage_root), week_id, snap["snapshot_id"], "weekly_chain"
    )
    return snap


def _quiet_dv(monkeypatch, cfg, before: dict, after: dict | None = None):
    """屏蔽数据面 I/O：注入固定 data_version（模拟 overlay state）。"""
    seq = [before, after if after is not None else before]

    def _fake_capture(c):
        return seq.pop(0) if len(seq) > 1 else seq[0]
    monkeypatch.setattr(st, "_capture_data_version", _fake_capture)
    # should_recompute 也用 _capture_data_version（同模块内引用，直接生效）


# ---------------------------------------------------------------------------
# 1. 正常周：双口径收益数字手算对照
# ---------------------------------------------------------------------------


class TestNormalWeek:
    def test_dual_basis_returns_match_hand_calc(self, cfg):
        """C1 平价 10→(周内 11/12/13/14/15)，open 首日=10.5。"""
        _publish_snapshot(cfg)
        closes = {d: 10.0 for d in SIGNAL_WEEK}
        closes[SIGNAL_DATE] = 10.0
        week_c = dict(zip(TRACK_WEEK, [11.0, 12.0, 13.0, 14.0, 15.0]))
        opens = {TRACK_WEEK[0]: 10.5}
        highs = {d: c + 1.0 for d, c in week_c.items()}
        lows = {d: c - 2.0 for d, c in week_c.items()}
        bars = _bars({**closes, **week_c}, opens=opens, highs=highs, lows=lows)
        # 基准：信号日收盘 100 → 首日**开盘** 100 → 周五收盘 110
        # （两口径锚点不同：sig=110/100-1=0.1；exec=110/100-1=0.1）
        bench_closes = {SIGNAL_DATE: 100.0, **{d: 110.0 for d in TRACK_WEEK}}
        bench = _bars(bench_closes, opens={TRACK_WEEK[0]: 100.0})
        out = _track(
            cfg,
            qfq={C1: bars, C2: bars},
            bench_arrays={BENCH: bench},
        )
        assert out["completion"] == sc.TRACK_COMPLETE
        row = next(r for r in _rows_of(out, cfg) if r["code"] == C1)
        # 手算：sig 口径 15/10-1 = 0.5；exec 口径 15/10.5-1
        assert row["entry_close_signal"] == 10.0      # 重读值，非快照 999
        assert row["entry_open_week"] == 10.5
        assert row["ret_close_sig"] == pytest.approx(0.5)
        assert row["ret_close_exec"] == pytest.approx(15 / 10.5 - 1)
        # 基准：sig 110/100-1=0.1；exec 110/100-1=0.1（首日 open=100）
        assert row["bench_ret_sig"] == pytest.approx(0.1)
        assert row["excess_sig"] == pytest.approx(0.5 - 0.1)
        assert row["excess_exec"] == pytest.approx((15 / 10.5 - 1) - 0.1)
        # 逐日：周一 close=11 → ret_vs_signal_close=0.1
        assert row["daily"][0]["ret_vs_signal_close"] == pytest.approx(0.1)
        assert len(row["daily"]) == 5
        # max_gain：sig=15/10-1=0.5（周五）；high=16 → ret_high_vs_signal=0.6
        assert row["max_gain_sig"] == pytest.approx(0.5)
        assert row["max_gain_sig_date"] == WEEK_END
        assert row["daily"][4]["ret_high_vs_signal"] == pytest.approx(0.6)
        # min_low：最低 low=9（周一）→ 9/10-1=-0.1
        assert row["min_low_ret_sig"] == pytest.approx(-0.1)
        assert row["status"] == "ok" and row["fill_status"] == sc.FILL_OK

    def test_rule_aggregation_and_coverage(self, cfg):
        """胜率/覆盖率/见顶日分布/平均回吐（分口径分母）。"""
        _publish_snapshot(cfg)
        # C1 涨（手算 0.5）、C2 跌（首日无 bar → no_bar 剔除 exec/sig? C2 整周停牌）
        c1 = _bars({
            SIGNAL_DATE: 10.0,
            TRACK_WEEK[0]: 10.0, TRACK_WEEK[1]: 12.0,
            TRACK_WEEK[2]: 13.0, TRACK_WEEK[3]: 11.0, TRACK_WEEK[4]: 15.0,
        })
        c2 = _bars({SIGNAL_DATE: 20.0})  # 跟踪周整周停牌
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 110.0 for d in TRACK_WEEK}})
        out = _track(
            cfg,
            qfq={C1: c1, C2: c2},
            bench_arrays={BENCH: bench},
        )
        stat = out_rule_stats(out, cfg)[0]
        assert stat["selected_count"] == 2
        assert stat["valid_sig_count"] == 1    # C2 无周内 bar → no_bar
        assert stat["valid_exec_count"] == 1
        assert stat["missing_count"] == 1
        assert stat["win_rate_sig"] == 1.0      # C1 ret=0.5 > 0
        # 覆盖率：sig 1/2、exec 1/2、excess 1/2（分母=selected）
        assert stat["coverage"][sc.RET_BASIS_SIGNAL] == 0.5
        assert stat["coverage"][sc.RET_BASIS_OPEN] == 0.5
        assert stat["coverage"]["excess"] == 0.5
        # C1 见顶周五（15 为最高，见顶日=max_gain_sig_date）
        row = next(r for r in _rows_of(out, cfg) if r["code"] == C1)
        assert row["max_gain_sig_date"] == WEEK_END
        # 回吐：max_gain_sig(0.5) - ret_close_sig(0.5) = 0
        assert stat["mean_giveback"] == 0.0


# ---------------------------------------------------------------------------
# 2. 复权安全：入场价从重读 bars 来，不是快照 close
# ---------------------------------------------------------------------------


class TestAdjustSafety:
    def test_entry_price_reread_not_snapshot_close(self, cfg):
        """快照 close=999 与重读 close=10 完全不同 → 收益必须按 10 算。"""
        _publish_snapshot(cfg, matched=[
            {"code": C1, "close": 999.0},
        ])
        bars = _bars({SIGNAL_DATE: 10.0, **{d: 10.0 for d in TRACK_WEEK}})
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 100.0 for d in TRACK_WEEK}})
        out = _track(cfg, qfq={C1: bars}, bench_arrays={BENCH: bench})
        row = _rows_of(out, cfg)[0]
        assert row["entry_close_signal"] == 10.0
        assert row["ret_close_sig"] == pytest.approx(0.0)
        # 若误用快照 999：ret 会是 10/999-1 ≈ -0.99 —— 断言远大于该值
        assert row["ret_close_sig"] > -0.9


# ---------------------------------------------------------------------------
# 3. 一字涨停 → limit_up_unbuyable，exec 收益 null 不进分母
# ---------------------------------------------------------------------------


class TestLimitUpUnbuyable:
    def test_one_word_limit_up_excluded_from_exec(self, cfg):
        """C2 首日一字涨停：涨停价=11（10*1.1 四舍五入），四价=11。"""
        _publish_snapshot(cfg, matched=[
            {"code": C1, "close": 10.0}, {"code": C2, "close": 10.0},
        ])
        c1 = _bars({SIGNAL_DATE: 10.0, **{d: 10.5 for d in TRACK_WEEK}})
        # qfq 面：复权价序列（信号日 10，首日一字 11.0=10*1.1）
        c2_qfq = _bars({
            SIGNAL_DATE: 10.0,
            TRACK_WEEK[0]: 11.0, TRACK_WEEK[1]: 11.5,
            TRACK_WEEK[2]: 11.5, TRACK_WEEK[3]: 11.5, TRACK_WEEK[4]: 12.0,
        })
        # raw 面：未复权一字涨停（prev_close=10 → 涨停价 11，O=H=L=C=11）
        c2_raw = _flat({
            SIGNAL_WEEK[-2]: 10.0, SIGNAL_DATE: 10.0,
            TRACK_WEEK[0]: 11.0, TRACK_WEEK[1]: 11.2,
            TRACK_WEEK[2]: 11.2, TRACK_WEEK[3]: 11.2, TRACK_WEEK[4]: 11.5,
        })
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 100.0 for d in TRACK_WEEK}})
        out = _track(
            cfg,
            qfq={C1: c1, C2: c2_qfq},
            raw={C1: c1, C2: c2_raw},
            bench_arrays={BENCH: bench},
        )

        rows = {r["code"]: r for r in _rows_of(out, cfg)}
        assert rows[C2]["fill_status"] == sc.FILL_LIMIT_UP_UNBUYABLE
        assert rows[C2]["ret_close_exec"] is None
        assert rows[C2]["ret_close_sig"] == pytest.approx(12 / 10 - 1)  # sig 口径照算
        # 理论开盘收益单独展示（不进分母）
        assert rows[C2]["theoretical_open_ret"] == pytest.approx(12 / 11 - 1)
        stat = out_rule_stats(out, cfg)[0]
        assert stat["selected_count"] == 2
        assert stat["valid_sig_count"] == 2       # sig 口径价格完整即可计算
        assert stat["valid_exec_count"] == 1      # unbuyable 剔除 exec 分母
        assert stat["unbuyable_count"] == 1
        assert stat["coverage"][sc.RET_BASIS_SIGNAL] == 1.0
        assert stat["coverage"][sc.RET_BASIS_OPEN] == 0.5


# ---------------------------------------------------------------------------
# 4. 停牌（首日无 bar）→ no_bar
# ---------------------------------------------------------------------------


class TestSuspension:
    def test_first_day_no_bar_is_no_bar(self, cfg):
        """C1 首日停牌后复牌：exec=no_bar，但 sig 口径照算（审查 🔴-2）。

        契约 §3：信号收盘口径价格完整即可计算——信号日收盘与窗末收盘
        都在，sig 收益/序列/最大涨幅必须完整；仅 exec（首日开盘锚）缺。
        复牌首根开盘只进 theoretical_open_ret 展示。
        """
        _publish_snapshot(cfg)
        bars = _bars({
            SIGNAL_DATE: 10.0,
            TRACK_WEEK[1]: 10.0, TRACK_WEEK[2]: 10.5,
            TRACK_WEEK[3]: 10.5, TRACK_WEEK[4]: 11.0,
        })
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 100.0 for d in TRACK_WEEK}})
        out = _track(cfg, qfq={C1: bars}, bench_arrays={BENCH: bench})
        row = _rows_of(out, cfg)[0]
        assert row["fill_status"] == sc.FILL_NO_BAR
        assert row["ret_close_exec"] is None
        assert row["entry_open_week"] is None
        # sig 口径完整：入场价重读 + 收益 + 逐日序列
        assert row["entry_close_signal"] == 10.0
        assert row["ret_close_sig"] == pytest.approx(11.0 / 10.0 - 1)
        assert row["status"] == "ok"
        assert len(row["daily"]) == 4  # 复牌后 4 根 bar
        # 复牌首根开盘（10.0）只进理论展示口径
        assert row["theoretical_open_ret"] == pytest.approx(11.0 / 10.0 - 1)
        stat = out_rule_stats(out, cfg)[0]
        # C1 sig 价格完整进分母；C2 全程无 bars → no_bar 不进任何分母
        assert stat["valid_sig_count"] == 1
        assert stat["valid_exec_count"] == 0
        assert stat["coverage"][sc.RET_BASIS_SIGNAL] == 0.5  # 1/2（C2 无数据）
        assert stat["coverage"][sc.RET_BASIS_OPEN] == 0.0


# ---------------------------------------------------------------------------
# 5. 整周休市 → no_trading_week 终态
# ---------------------------------------------------------------------------


class TestNoTradingWeek:
    def test_no_trading_week_is_terminal(self, cfg, monkeypatch):
        """跟踪周整周休市：TaskState 记 no_trading_week，再次判定不再算。

        日历必须**已覆盖**该周（例：春节整周休市——日历含该周之后的日期，
        只是这一周没有交易日）。若日历根本没到那一周，语义是 pending
        （window 未结束）而不是 no_trading_week（见下一条用例）。
        """
        _publish_snapshot(cfg)
        # 审查 🟡 回归锁：终态同样清抢锁欠账（周键 + 快照身份键两个体系）
        week_key = st.week_task_key(SIGNAL_DATE)
        snap_key = st.tracking_task_key("snap_20250110_test")
        sc.record_pending_job(Path(cfg.storage_root), week_key, reason="skipped_locked")
        sc.record_pending_job(Path(cfg.storage_root), snap_key, reason="skipped_locked")
        # 日历：信号周 + 跟踪周之后还有交易日（覆盖到了），但跟踪周本身
        # 一个交易日都没有 → 真正的"整周休市"
        cal = TradeCalendar(list(SIGNAL_WEEK) + [20250127])
        bars = _bars({SIGNAL_DATE: 10.0})
        out = st.compute_weekly_tracking(
            cfg, SIGNAL_DATE,
            bar_loader=_loader({C1: bars}),
            calendar=cal,
        )
        assert out["completion"] == sc.TRACK_NO_TRADING_WEEK
        state = sc.load_task_state(
            Path(cfg.storage_root), st.tracking_task_key("snap_20250110_test")
        )
        assert state["completion"] == sc.TRACK_NO_TRADING_WEEK
        # 终态清欠账：两个体系的键都不残留
        assert sc.load_pending_jobs(Path(cfg.storage_root))["jobs"] == {}
        # 终态短路：数据面无论多新都不再重算（补偿判定 False）
        need, reason = st.should_recompute(
            cfg, SIGNAL_DATE, calendar=cal,
            bar_loader=_loader({C1: _bars({SIGNAL_DATE: 10.0, **{d: 10 for d in [20250120]}})}),
        )
        assert need is False

    def test_calendar_not_covering_track_week_is_pending(self, cfg):
        """★真实数据实测修正：日历还没到跟踪周（行情只到信号周）→ pending。

        误判成 no_trading_week（终态）会让该周永久不再结算——正常周五链
        遇行情滞后即踩此坑。
        """
        _publish_snapshot(cfg)
        cal = TradeCalendar(list(SIGNAL_WEEK))  # 只到信号周，未覆盖下一周
        out = st.compute_weekly_tracking(
            cfg, SIGNAL_DATE,
            bar_loader=_loader({C1: _bars({SIGNAL_DATE: 10.0})}),
            calendar=cal,
        )
        assert out["completion"] == sc.TRACK_PENDING
        assert out["reason"] == "track_week_not_covered_yet"
        # 不写终态 TaskState：到期（行情补上）必须能重算
        state = sc.load_task_state(
            Path(cfg.storage_root), st.tracking_task_key("snap_20250110_test")
        )
        assert state is None or state.get("completion") != sc.TRACK_NO_TRADING_WEEK

    def test_misjudged_terminal_self_heals(self, cfg):
        """★真实数据事故回归（2026-09-15）：误判的终态必须能自愈重算。

        生产实况：9/11 周被写成 no_trading_week 终态，但该周下一个自然周
        （9/14~9/18）明明有交易日——写盘时的日历视图是"越过该周却整周无
        交易日"（稀疏日历/数据面中间态），属误判。而 should_complete_further
        对 no_trading_week 恒 False（契约：日历推导的合法终态），不在
        should_recompute 层拦截，该周就被永久锁死——补偿机制再也捞不回，
        周五链也不会再结算它。
        """
        _publish_snapshot(cfg)
        # ① 误判现场：日历"越过"跟踪周（含更晚日期）但跟踪周内一整周无交易日
        bad_cal = TradeCalendar(list(SIGNAL_WEEK) + [20250124])
        bars = {C1: _bars({SIGNAL_DATE: 10.0, **{d: 10.0 for d in TRACK_WEEK}})}
        bench = {BENCH: _flat({SIGNAL_DATE: 100.0, **{d: 100.0 for d in TRACK_WEEK}})}
        out = _track(cfg, qfq=bars, bench_arrays=bench, calendar=bad_cal)
        assert out["completion"] == sc.TRACK_NO_TRADING_WEEK
        # ② 日历修正（跟踪周交易日补齐）→ 终态不成立，判定必须恢复重算
        need, reason = st.should_recompute(
            cfg, SIGNAL_DATE, calendar=_calendar(),
            bar_loader=_loader(bars),
        )
        assert need is True
        assert reason == "no_trading_week_recovered"
        # ③ 重算真正落地：正常结算覆盖误判终态（不是只在判定层 True）
        out2 = _track(cfg, qfq=bars, bench_arrays=bench)
        assert out2["completion"] == sc.TRACK_COMPLETE
        state = sc.load_task_state(
            Path(cfg.storage_root), st.tracking_task_key("snap_20250110_test")
        )
        assert state["completion"] == sc.TRACK_COMPLETE


# ---------------------------------------------------------------------------
# 6. 空选（selected=0）→ 胜率 null 而非 0；覆盖率 null
# ---------------------------------------------------------------------------


class TestEmptySelection:
    def test_empty_week_null_rates(self, cfg):
        """规则命中 0 票：胜率/收益率 null（不是 0），覆盖率 null（不是 0/1）。"""
        _publish_snapshot(cfg, matched=[])
        bars = _bars({SIGNAL_DATE: 10.0, **{d: 10.0 for d in TRACK_WEEK}})
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 100.0 for d in TRACK_WEEK}})
        out = _track(cfg, qfq={}, bench_arrays={BENCH: bench})
        assert out["completion"] == sc.TRACK_COMPLETE  # 空仓也是完整结算
        stat = out_rule_stats(out, cfg)[0]
        assert stat["selected_count"] == 0
        assert stat["win_rate_sig"] is None
        assert stat["win_rate_exec"] is None
        assert stat["coverage"][sc.RET_BASIS_SIGNAL] is None
        assert stat["coverage"][sc.RET_BASIS_OPEN] is None
        assert stat["coverage"]["excess"] is None
        assert out["coverage"][sc.RET_BASIS_SIGNAL] is None


# ---------------------------------------------------------------------------
# 7. 基准缺 → blocked_benchmark + 股票收益保留；补齐后新 revision
# ---------------------------------------------------------------------------


class TestBenchmark:
    def test_blocked_benchmark_then_new_revision_on_fill(self, cfg, monkeypatch):
        _publish_snapshot(cfg)
        bars = _bars({SIGNAL_DATE: 10.0, **{d: 11.0 for d in TRACK_WEEK}})
        _quiet_dv(monkeypatch, cfg, DV1)
        out1 = _track(cfg, qfq={C1: bars}, bench_arrays={})  # 基准缺
        assert out1["completion"] == sc.TRACK_BLOCKED_BENCHMARK
        # 股票收益已保留在产物里（excess=null）
        p1 = json.loads(Path(out1["product_path"]).read_text(encoding="utf-8"))
        row = p1["rows"][0]
        assert row["ret_close_sig"] == pytest.approx(0.1)
        assert row["excess_sig"] is None
        assert p1["benchmark"]["available"] is False
        # 基准补齐（benchmark_loader 换有数据的）→ 新 revision 重算成功
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 110.0 for d in TRACK_WEEK}})
        bench_loader = _loader({BENCH: bench}, data_version={"dataset_id": "idx_v2"})
        out2 = st.compute_weekly_tracking(
            cfg, SIGNAL_DATE,
            bar_loader=_loader({C1: bars}),
            raw_loader=_loader({C1: bars}),
            benchmark_loader=bench_loader,
            calendar=_calendar(),
        )
        assert out2["completion"] == sc.TRACK_COMPLETE
        assert out2["tracking_revision_id"] != out1["tracking_revision_id"]
        # 旧产物字节不动（并列保留）
        assert Path(out1["product_path"]).exists()
        p2 = json.loads(Path(out2["product_path"]).read_text(encoding="utf-8"))
        assert p2["rows"][0]["excess_sig"] == pytest.approx(0.1 - 0.1)
        assert p2["benchmark"]["available"] is True


# ---------------------------------------------------------------------------
# 8. 算法版本升级 → 新 revision，旧文件字节不变
# ---------------------------------------------------------------------------


class TestAlgoVersionBump:
    def test_algo_bump_new_revision_old_file_untouched(self, cfg, monkeypatch):
        _publish_snapshot(cfg)
        bars = _bars({SIGNAL_DATE: 10.0, **{d: 10.0 for d in TRACK_WEEK}})
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 100.0 for d in TRACK_WEEK}})
        _quiet_dv(monkeypatch, cfg, DV1)
        out1 = _track(cfg, qfq={C1: bars}, bench_arrays={BENCH: bench})
        rev1 = out1["tracking_revision_id"]
        old_path = Path(out1["product_path"])
        before_bytes = old_path.read_bytes()
        before_mtime = old_path.stat().st_mtime_ns

        out2 = _track(
            cfg, qfq={C1: bars}, bench_arrays={BENCH: bench}, algo_version="2"
        )
        rev2 = out2["tracking_revision_id"]
        assert rev1 != rev2
        # 旧文件字节与 mtime 都不变（绝不覆盖）
        assert old_path.read_bytes() == before_bytes
        assert old_path.stat().st_mtime_ns == before_mtime
        # 期望 revision = 契约原语手算（bench 注入无 data_version meta 时
        # 面身份回退 "unavailable"）；schema 用常量（bump 时本用例不该脆断，
        # 它锁定的是"algo 版本参与 revision 身份"这个语义）
        expect = sc.tracking_revision_id(
            "snap_20250110_test", DV1, {"benchmark": "unavailable"},
            "2", st.TRACKING_SCHEMA_VERSION,
        )
        assert rev2 == expect
        # current 指针指向新 revision
        pointer = json.loads(
            sc.track_current_path(Path(cfg.storage_root), "snap_20250110_test")
            .read_text(encoding="utf-8")
        )
        assert pointer["tracking_revision_id"] == rev2


# ---------------------------------------------------------------------------
# 9. commit_seq 变化（数据版本升级）→ revision 变化
# ---------------------------------------------------------------------------


class TestDataVersionBump:
    def test_commit_seq_change_new_revision(self, cfg, monkeypatch):
        _publish_snapshot(cfg)
        bars = _bars({SIGNAL_DATE: 10.0, **{d: 10.0 for d in TRACK_WEEK}})
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 100.0 for d in TRACK_WEEK}})
        # 第一次：DV1（前后一致）
        _quiet_dv(monkeypatch, cfg, DV1)
        out1 = _track(cfg, qfq={C1: bars}, bench_arrays={BENCH: bench})
        # 第二次：DV2（仅 commit_seq 变）
        monkeypatch.setattr(
            st, "_capture_data_version", lambda c: dict(DV2)
        )
        out2 = _track(cfg, qfq={C1: bars}, bench_arrays={BENCH: bench})
        assert out1["tracking_revision_id"] != out2["tracking_revision_id"]
        # 两份产物并列存在
        assert Path(out1["product_path"]).exists()
        assert Path(out2["product_path"]).exists()

    def test_data_version_change_mid_read_aborts(self, cfg, monkeypatch):
        """固定读版本复查不一致 → data_version_changed，不落产物。"""
        _publish_snapshot(cfg)
        bars = _bars({SIGNAL_DATE: 10.0, **{d: 10.0 for d in TRACK_WEEK}})
        # 第一次捕获 DV1，批读后复查 DV2 → 不一致
        _quiet_dv(monkeypatch, cfg, DV1, after=DV2)
        out = _track(cfg, qfq={C1: bars}, bench_arrays={})
        assert out["completion"] == sc.TRACK_DATA_VERSION_CHANGED
        assert "product_path" not in out  # 不落产物
        # 状态文件没有转向终态
        state = sc.load_task_state(
            Path(cfg.storage_root), st.tracking_task_key("snap_20250110_test")
        )
        assert state is None or state.get("completion") != sc.TRACK_COMPLETE


# ---------------------------------------------------------------------------
# 10. CLI 退出码映射 + no_snapshot
# ---------------------------------------------------------------------------


class TestCliExitCodes:
    def test_no_snapshot_exit_2(self, cfg):
        """无发布快照周 → completion=no_snapshot → 退出码 2。"""
        out = st.compute_weekly_tracking(
            cfg, SIGNAL_DATE,
            bar_loader=_loader({}),
            raw_loader=_loader({}),
            benchmark_loader=_loader({}),
            calendar=_calendar(),
        )
        assert out["completion"] == "no_snapshot"
        assert out["reason"] == "no_published_snapshot"

    def test_exit_code_mapping(self):
        """契约 §1 退出码映射（CLI 包装层核心，纯函数）。"""
        from wtpy.apps.astock.cli import track_exit_code_for_completion as m
        assert m(sc.TRACK_COMPLETE) == 0
        assert m(sc.TRACK_NO_TRADING_WEEK) == 0
        assert m(sc.TRACK_PENDING) == 3
        assert m(sc.TRACK_BLOCKED_BENCHMARK) == 3
        assert m(sc.TRACK_DATA_VERSION_CHANGED) == 3
        assert m("no_snapshot") == 2
        assert m(sc.TRACK_FAILED) == 1
        assert m("whatever") == 1

    def test_no_snapshot_via_cli(self, cfg, monkeypatch, capsys):
        """CLI 单周模式：无快照 → 2（monkeypatch 服务函数测包装层）。"""
        from wtpy.apps.astock import cli as cli_mod

        empty_cfg = cfg  # 未发布任何快照
        args = cli_mod.build_parser().parse_args(
            ["--storage", str(empty_cfg.storage_root), "track-weekly",
             "--week", str(SIGNAL_DATE)]
        )
        rc = cli_mod.cmd_track_weekly(args)
        captured = capsys.readouterr()
        assert rc == 2
        # CLI 输出 JSON 摘要：语义断言（completion/no_snapshot 原因）
        payload = json.loads(_last_json(captured.out))
        assert payload["completion"] == "no_snapshot"
        assert payload["reason"] == "no_published_snapshot"

    def test_complete_via_cli_exit_0(self, cfg, monkeypatch, capsys):
        """monkeypatch compute_weekly_tracking → complete：CLI 返回 0。"""
        from wtpy.apps.astock import cli as cli_mod
        _publish_snapshot(cfg)
        fake_out = {
            "completion": sc.TRACK_COMPLETE,
            "week_id": SIGNAL_DATE,
            "tracking_revision_id": "rev_x",
        }
        monkeypatch.setattr(
            cli_mod.tracksvc, "compute_weekly_tracking",
            lambda c, w, **k: dict(fake_out),
        )
        # resolve 默认周需要 latest snapshot：已发布，正常解析
        args = cli_mod.build_parser().parse_args(
            ["--storage", str(cfg.storage_root), "track-weekly"]
        )
        rc = cli_mod.cmd_track_weekly(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert json.loads(_last_json(out))["completion"] == sc.TRACK_COMPLETE

    def test_blocked_benchmark_via_cli_exit_3(self, cfg, monkeypatch, capsys):
        """monkeypatch → blocked_benchmark：CLI 返回 3，不重试。"""
        from wtpy.apps.astock import cli as cli_mod
        _publish_snapshot(cfg)
        calls = {"n": 0}

        def _fake(c, w, **k):
            calls["n"] += 1
            return {"completion": sc.TRACK_BLOCKED_BENCHMARK, "week_id": w}

        monkeypatch.setattr(cli_mod.tracksvc, "compute_weekly_tracking", _fake)
        args = cli_mod.build_parser().parse_args(
            ["--storage", str(cfg.storage_root), "track-weekly"]
        )
        rc = cli_mod.cmd_track_weekly(args)
        assert rc == 3
        assert calls["n"] == 1  # blocked 不触发重试（只有 data_version_changed 才重试）

    def test_data_version_changed_retries_once(self, cfg, monkeypatch, capsys):
        """data_version_changed：自动重试一次；再失败 → 3。"""
        from wtpy.apps.astock import cli as cli_mod
        _publish_snapshot(cfg)
        calls = {"n": 0}

        def _fake(c, w, **k):
            calls["n"] += 1
            return {"completion": sc.TRACK_DATA_VERSION_CHANGED, "week_id": w}

        monkeypatch.setattr(cli_mod.tracksvc, "compute_weekly_tracking", _fake)
        args = cli_mod.build_parser().parse_args(
            ["--storage", str(cfg.storage_root), "track-weekly"]
        )
        rc = cli_mod.cmd_track_weekly(args)
        assert rc == 3
        assert calls["n"] == 2  # 首次 + 重试一次

    def test_data_version_changed_retry_then_ok(self, cfg, monkeypatch, capsys):
        """重试后成功 → 退出码 0（对 capsys 的 json 末段断言）。"""
        from wtpy.apps.astock import cli as cli_mod
        _publish_snapshot(cfg)
        calls = {"n": 0}

        def _fake(c, w, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"completion": sc.TRACK_DATA_VERSION_CHANGED, "week_id": w}
            return {"completion": sc.TRACK_COMPLETE, "week_id": w,
                    "tracking_revision_id": "r"}

        monkeypatch.setattr(cli_mod.tracksvc, "compute_weekly_tracking", _fake)
        args = cli_mod.build_parser().parse_args(
            ["--storage", str(cfg.storage_root), "track-weekly"]
        )
        rc = cli_mod.cmd_track_weekly(args)
        assert rc == 0
        assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 补充：同 revision 复用不覆盖 / 窗口未结束 pending / 回填周枚举
# ---------------------------------------------------------------------------


class TestProductImmutability:
    def test_same_revision_reuse_not_overwrite(self, cfg, monkeypatch):
        """同 revision 二次计算 → 校验内容一致复用，不覆盖（mtime 不变）。"""
        _publish_snapshot(cfg)
        bars = _bars({SIGNAL_DATE: 10.0, **{d: 10.0 for d in TRACK_WEEK}})
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 100.0 for d in TRACK_WEEK}})
        _quiet_dv(monkeypatch, cfg, DV1)
        out1 = _track(cfg, qfq={C1: bars}, bench_arrays={BENCH: bench})
        path = Path(out1["product_path"])
        before = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns
        out2 = _track(cfg, qfq={C1: bars}, bench_arrays={BENCH: bench})
        assert out2["product_reused"] is True
        assert out2["product_path"] == str(path)
        assert path.read_bytes() == before
        assert path.stat().st_mtime_ns == before_mtime


class TestPendingWindow:
    def test_window_not_ended_pending(self, cfg):
        """数据面 max_date < 周五 → 票标 pending、completion=pending（退出码 3）。"""
        _publish_snapshot(cfg)
        bars = _bars({SIGNAL_DATE: 10.0, TRACK_WEEK[0]: 10.0, TRACK_WEEK[1]: 10.5})
        bench = _flat({SIGNAL_DATE: 100.0, TRACK_WEEK[0]: 100.0, TRACK_WEEK[1]: 100.5})
        # 日历跟踪周只到周三（窗口未结束）：跟踪周 3 天
        cal = TradeCalendar(SIGNAL_WEEK + TRACK_WEEK[:3])
        out = st.compute_weekly_tracking(
            cfg, SIGNAL_DATE,
            bar_loader=_loader({C1: bars, BENCH: bench}),
            raw_loader=_loader({C1: bars, BENCH: bench}),
            benchmark_loader=_loader({BENCH: bench}),
            calendar=cal,
        )
        assert out["completion"] == sc.TRACK_PENDING
        assert out["window_ended"] is False
        rows = _rows_of(out, cfg)
        assert rows[0]["status"] == "pending"
        stat = out_rule_stats(out, cfg)[0]
        assert stat["pending_count"] == 1
        assert stat["valid_sig_count"] == 0  # pending 不进 valid 分母


class TestPastSignalWeeks:
    def test_backfill_weeks_enumeration(self, cfg):
        """过去 N 个周五按 ISO 周历回退；整周休市周不产生 week_id。"""
        cal = TradeCalendar(list(ALL_DATES) + [20250120])
        weeks = st.past_signal_weeks(cal, SIGNAL_DATE, 2)
        # anchor 周 + 前一周（2024-12-30~2025-01-03 无交易日 → 跳过）
        assert weeks[0] == SIGNAL_DATE
        assert all(w != 20241231 for w in weeks)  # 前一周整周休市不顶替


class TestReaderCompatibility:
    def test_product_fields_for_stage3_reader(self, cfg):
        """产物字段与阶段 3 读取方（api_routes/tracking.py）兼容：

        completion / rule_aggregates / mean_ret_close_sig|exec /
        rows[].code|rule_id|ret_close_*|fill_status / coverage 三口径。
        """
        _publish_snapshot(cfg)
        # 注意：恒等复权世界里 10→11 恰好是 10% 一字涨停（O=H=L=C=11），
        # 会被 limit_rules 正确判为 unbuyable → exec=null。给首日一个
        # 非涨停开盘 open=10.8，验证 exec 路径正常。
        bars = _bars(
            {SIGNAL_DATE: 10.0, **{d: 11.0 for d in TRACK_WEEK}},
            opens={TRACK_WEEK[0]: 10.8},
            lows={TRACK_WEEK[0]: 10.5},
        )
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 100.0 for d in TRACK_WEEK}})
        out = _track(cfg, qfq={C1: bars}, bench_arrays={BENCH: bench})
        p = json.loads(Path(out["product_path"]).read_text(encoding="utf-8"))
        assert p["completion"] == sc.TRACK_COMPLETE
        aggs = p["rule_aggregates"]
        assert len(aggs) == 1 and aggs[0]["rule_id"] == "r1"
        assert aggs[0]["mean_ret_close_sig"] == pytest.approx(0.1)
        # 首日 open=10.8 → exec 收益 = 11/10.8-1（产物按 6 位小数存储）
        assert aggs[0]["mean_ret_close_exec"] == pytest.approx(round(11 / 10.8 - 1, 6))
        assert p["rows"][0]["fill_status"] == sc.FILL_OK
        assert "mean_excess_sig" in aggs[0]
        row = p["rows"][0]
        for k in ("code", "rule_id", "ret_close_sig", "ret_close_exec",
                  "fill_status", "daily"):
            assert k in row
        assert set(p["coverage"].keys()) == {
            sc.RET_BASIS_SIGNAL, sc.RET_BASIS_OPEN, "excess"
        }

    def test_v2_rows_carry_name_and_week_end_close(self, cfg, monkeypatch):
        """schema v2：行内 name（结算时解析）+ close_week_end（周五收盘价）。

        name 走 stock_names 解析链（tmp 环境无名称源 → 空串），这里用替身
        验证"结算层确实把解析结果写进了产物行"；外部名称源本身由
        stock_names 自己的测试覆盖，不在此重复。
        """
        from wtpy.apps.astock.service import stock_names as sn

        monkeypatch.setattr(
            sn,
            "resolve_stock_name",
            lambda _cfg, code, **_kw: {"000001": "平安银行"}.get(str(code), ""),
        )
        _publish_snapshot(cfg)
        bars = _bars({SIGNAL_DATE: 10.0, **{d: 15.0 for d in TRACK_WEEK}})
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 100.0 for d in TRACK_WEEK}})
        out = _track(cfg, qfq={C1: bars}, bench_arrays={BENCH: bench})
        p = json.loads(Path(out["product_path"]).read_text(encoding="utf-8"))
        assert p["schema_version"] == st.TRACKING_SCHEMA_VERSION
        row = next(r for r in p["rows"] if r["code"] == C1)
        assert row["name"] == "平安银行"
        # 窗末收盘 = 跟踪周最后交易日的重读收盘（不能再让前端从 daily 反推）
        assert row["close_week_end"] == pytest.approx(15.0)

    def test_v2_missing_name_is_empty_not_fabricated(self, cfg, monkeypatch):
        """名称解析失败/缺名 → 空串（不是编造的代码或占位符）。"""
        from wtpy.apps.astock.service import stock_names as sn

        monkeypatch.setattr(
            sn, "resolve_stock_name",
            lambda _cfg, _code, **_kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        _publish_snapshot(cfg)
        bars = _bars({SIGNAL_DATE: 10.0, **{d: 15.0 for d in TRACK_WEEK}})
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 100.0 for d in TRACK_WEEK}})
        out = _track(cfg, qfq={C1: bars}, bench_arrays={BENCH: bench})
        p = json.loads(Path(out["product_path"]).read_text(encoding="utf-8"))
        row = next(r for r in p["rows"] if r["code"] == C1)
        assert row["name"] == ""
        # 缺名不阻塞结算：产物照常 complete
        assert p["completion"] == sc.TRACK_COMPLETE


class TestShouldRecompute:
    def test_complete_matching_revision_no_recompute(self, cfg, monkeypatch):
        """终态 + revision 一致 → 不重算（算法升级前不空转）。"""
        _publish_snapshot(cfg)
        bars = _bars({SIGNAL_DATE: 10.0, **{d: 10.0 for d in TRACK_WEEK}})
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 100.0 for d in TRACK_WEEK}})
        _quiet_dv(monkeypatch, cfg, DV1)
        _track(cfg, qfq={C1: bars}, bench_arrays={BENCH: bench})
        need, reason = st.should_recompute(
            cfg, SIGNAL_DATE,
            calendar=_calendar(),
            bar_loader=_loader({C1: bars}),
            benchmark_loader=_loader({BENCH: bench}),
        )
        assert need is False
        assert "revision" in reason or reason == "complete_and_revision_matches"

    def test_algo_upgrade_triggers_recompute(self, cfg, monkeypatch):
        """算法升级 → should_recompute 目标 revision 捞回陈旧 complete。"""
        _publish_snapshot(cfg)
        bars = _bars({SIGNAL_DATE: 10.0, **{d: 10.0 for d in TRACK_WEEK}})
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 100.0 for d in TRACK_WEEK}})
        _quiet_dv(monkeypatch, cfg, DV1)
        _track(cfg, qfq={C1: bars}, bench_arrays={BENCH: bench})
        need, reason = st.should_recompute(
            cfg, SIGNAL_DATE, algo_version="2",
            calendar=_calendar(),
            bar_loader=_loader({C1: bars}),
        )
        assert need is True


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _rows_of(out: dict, cfg) -> list:
    """从产物文件读 rows（保证断言针对落盘内容而非内存对象）。"""
    p = Path(out["product_path"])
    return json.loads(p.read_text(encoding="utf-8"))["rows"]


def out_rule_stats(out: dict, cfg) -> list:
    p = Path(out["product_path"])
    return json.loads(p.read_text(encoding="utf-8"))["rules"]


def _last_json(text: str) -> str:
    """取 stdout 的最后一个 JSON 对象（CLI 摘要在日志行之后）。"""
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                last = text[start:i + 1]
    return last


class TestBenchmarkBoundary:
    """审查 🔴-3 回归锁：基准锚点必须精确落在窗口边界。"""

    def test_lagged_benchmark_blocks_not_mismatched_excess(self, cfg, monkeypatch):
        """指数面只同步到周四：不得用周四收益冒充"周五基准"产出错配 excess。

        正确行为：benchmark_ready=False（boundary_mismatch）→ 股票收益
        保留、excess=null、completion=blocked_benchmark。
        """
        _publish_snapshot(cfg)
        bars = _bars({SIGNAL_DATE: 10.0, **{d: 11.0 for d in TRACK_WEEK}})
        # 基准指数只到周四（缺周五）：旧实现会把周四收盘当"窗末"算 excess
        lagged = _flat({SIGNAL_DATE: 100.0, **{d: 105.0 for d in TRACK_WEEK[:4]}})
        out = _track(cfg, qfq={C1: bars}, bench_arrays={BENCH: lagged})
        assert out["completion"] == sc.TRACK_BLOCKED_BENCHMARK
        p = json.loads(Path(out["product_path"]).read_text(encoding="utf-8"))
        # 股票收益保留
        assert p["rows"][0]["ret_close_sig"] == pytest.approx(0.1)
        # 基准不可用 + 原因落盘
        assert p["benchmark"]["available"] is False
        assert p["benchmark"]["reason"] == "boundary_mismatch"
        assert p["rows"][0]["excess_sig"] is None

    def test_benchmark_missing_first_day_blocks(self, cfg):
        """基准缺首日（周二起）：同样 boundary_mismatch，不冒充首日开盘。"""
        _publish_snapshot(cfg)
        bars = _bars({SIGNAL_DATE: 10.0, **{d: 11.0 for d in TRACK_WEEK}})
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 110.0 for d in TRACK_WEEK[1:]}})
        out = _track(cfg, qfq={C1: bars}, bench_arrays={BENCH: bench})
        assert out["completion"] == sc.TRACK_BLOCKED_BENCHMARK
        p = json.loads(Path(out["product_path"]).read_text(encoding="utf-8"))
        assert p["benchmark"]["available"] is False
        assert p["benchmark"]["reason"] == "boundary_mismatch"


class TestUnknownFillBasis:
    """审查测试缺口：unknown（判定元数据不足）不进开盘口径分母。

    实现路径：fill=unknown 时 ret_close_exec=None → unit_exec 不成立 →
    不进 valid_exec / exec 胜率分母；unknown_count 单独记账可见。
    触发方式：raw 面缺"首日前收"（首日前一根无 bar）→ _limit_fill_status
    返回 unknown（绝不默认 ok）。
    """

    def test_unknown_excluded_from_exec_but_counted(self, cfg):
        _publish_snapshot(cfg, matched=[
            {"code": C1, "close": 10.0}, {"code": C2, "close": 10.0},
        ])
        # C1 上涨（exec 口径正收益，用于验证"胜率只由可成交票决定"）
        c1 = _bars({
            SIGNAL_DATE: 10.0, TRACK_WEEK[0]: 10.0,
            TRACK_WEEK[1]: 10.5, TRACK_WEEK[2]: 10.5,
            TRACK_WEEK[3]: 10.8, TRACK_WEEK[4]: 11.0,
        })
        c2_qfq = _bars({SIGNAL_DATE: 10.0, **{d: 10.5 for d in TRACK_WEEK}})
        # raw 面：只有跟踪周首日之后的行，缺首日（也无前收）→ 判定元数据不足
        c2_raw = _flat({d: 10.5 for d in TRACK_WEEK})
        bench = _flat({SIGNAL_DATE: 100.0, **{d: 100.0 for d in TRACK_WEEK}})
        out = _track(
            cfg,
            qfq={C1: c1, C2: c2_qfq},
            raw={C1: c1, C2: c2_raw},
            bench_arrays={BENCH: bench},
        )
        rows = {r["code"]: r for r in _rows_of(out, cfg)}
        assert rows[C2]["fill_status"] == sc.FILL_UNKNOWN
        # 开盘口径收益为 null（不按假设价算），信号口径照算
        assert rows[C2]["ret_close_exec"] is None
        assert rows[C2]["ret_close_sig"] is not None
        assert rows[C2]["excess_exec"] is None

        stat = out_rule_stats(out, cfg)[0]
        assert stat["unknown_count"] == 1
        # C1 可成交（1），C2 unknown → 不进 exec 分母
        assert stat["valid_exec_count"] == 1
        assert stat["valid_sig_count"] == 2
        # exec 胜率只由 C1 决定
        assert stat["win_rate_exec"] == 1.0
        assert stat["coverage"][sc.RET_BASIS_OPEN] == pytest.approx(0.5)
        assert stat["coverage"][sc.RET_BASIS_SIGNAL] == pytest.approx(1.0)
