"""周五链指标复核（indicator_review）单测。

合成 bars 走 指标/ 目录真实公式（txt_735金叉及趋势 / txt_先跌后涨新版5日外），
验证命中判定、asof 过滤、幂等、force、no_go、JSON 结构与 CLI 冒烟。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.service import indicator_review as ir

ASOF = 20260828  # 周五

# 735 命中序列：40 天平 10.0 + 20 天缓涨 0.01/天 -> 末日 TJ2（MA7>MA35 双升 DEV<=2）
RAMP_CLOSES = [10.0] * 40 + [round(10.0 + 0.01 * i, 4) for i in range(1, 21)]
# 无命中序列：单边下跌
DOWN_CLOSES = [round(15.0 - 0.05 * i, 4) for i in range(60)]
# 5日外命中序列：随机搜索得到的 88 根合成 K 线，XG 仅在最后一根为真
# （先涨->跌->缓涨->急涨->小回撤->末段急涨，满足 MA5X28/MA7X35/M60_DTG/
#   MTM_DTG/HIST_TURN/ABOVE_MA 全部条件）
GAP_CLOSES = [
    10.0938, 10.1876, 10.2814, 10.3752, 10.469, 10.5628, 10.6566, 10.7504,
    10.8442, 10.938, 11.0318, 11.1256, 11.2193, 11.3131, 11.4069, 11.5007,
    11.5945, 11.6883, 11.7821, 11.8759, 11.7271, 11.5783, 11.4294, 11.2806,
    11.1318, 10.983, 10.8342, 10.6853, 10.5365, 10.3877, 10.2389, 10.09,
    10.1365, 10.183, 10.2295, 10.276, 10.3225, 10.369, 10.4155, 10.462,
    10.5085, 10.555, 10.6015, 10.648, 10.6945, 10.741, 10.7875, 10.8339,
    10.8804, 10.9269, 10.9734, 11.1026, 11.2318, 11.3609, 11.4901, 11.6192,
    11.7484, 11.8776, 12.0067, 12.1359, 12.2651, 12.3942, 12.5234, 12.6526,
    12.7817, 12.9109, 13.0401, 13.1692, 13.2984, 13.4275, 13.5567, 13.4578,
    13.359, 13.2601, 13.1612, 13.0623, 12.9635, 12.8646, 12.7657, 12.8797,
    12.9936, 13.1076, 13.2216, 13.3355, 13.4495, 13.5634, 13.6774, 13.7913,
]


def _weekday_dates(n: int, end: int = ASOF) -> list[int]:
    """生成以 end（周五）收尾、往前推的 n 个工作日 YYYYMMDD 序列。"""
    d = dt.datetime.strptime(str(end), "%Y%m%d").date()
    out: list[int] = []
    while len(out) < n:
        if d.isoweekday() <= 5:
            out.append(int(d.strftime("%Y%m%d")))
        d -= dt.timedelta(days=1)
    return list(reversed(out))


def _bars(closes: list[float], dates: list[int]) -> list[DayBar]:
    return [
        DayBar(
            date=d,
            open=c,
            high=round(c + 0.01, 4),
            low=round(c - 0.01, 4),
            close=c,
            amount=1e7,
            volume=1e6,
        )
        for d, c in zip(dates, closes)
    ]


BARS_BY_CODE = {
    "SSE.STK.600000": _bars(RAMP_CLOSES, _weekday_dates(len(RAMP_CLOSES))),
    "SSE.STK.600001": _bars(GAP_CLOSES, _weekday_dates(len(GAP_CLOSES))),
    "SSE.STK.600002": _bars(DOWN_CLOSES, _weekday_dates(len(DOWN_CLOSES))),
    # 停牌票：asof 前已无 K 线
    "SSE.STK.600003": _bars(DOWN_CLOSES[:30], _weekday_dates(30, end=20260820)),
}


def _fake_loader(code: str, asof: int):
    bars = BARS_BY_CODE.get(code)
    if bars is None:
        raise FileNotFoundError(f"no bars for {code}")
    trimmed = [b for b in bars if int(b.date) <= asof]
    return trimmed, {"dataset_id": "mock_l1"}


def _ok_surface(cfg):
    return {"formal_l1_id": "ds_mock_l1", "max_date": ASOF}, ""


def _cfg(tmp_path):
    return get_default_config(storage_root=tmp_path)


def test_review_hit_judgment_and_json(tmp_path):
    """735 命中缓涨票、5日外命中 V 形票；无命中/停牌票不进 matched。"""
    cfg = _cfg(tmp_path)
    summary = ir.run_weekly_review(
        cfg,
        asof=ASOF,
        codes=list(BARS_BY_CODE),
        bar_loader=_fake_loader,
        surface_resolver=_ok_surface,
    )
    assert summary["status"] == "ok"
    assert summary["asof"] == ASOF
    assert summary["universe_size"] == 4
    assert summary["scanned"] == 4
    by_sheet = {r["sheet"]: r for r in summary["rules"]}
    assert by_sheet["735"]["count"] == 1
    assert by_sheet["735"]["matched"] == [{"code": "SSE.STK.600000", "close": 10.2}]
    assert by_sheet["5日外"]["count"] == 1
    assert by_sheet["5日外"]["matched"][0]["code"] == "SSE.STK.600001"
    # 落盘 JSON 与返回摘要一致，键齐全
    path = ir.review_output_path(cfg, ASOF)
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["status"] == "ok"
    for key in (
        "asof", "generated_at", "status", "no_go_reason", "universe_size",
        "scanned", "error_count", "rules", "duration_sec",
    ):
        assert key in on_disk


def test_review_asof_filter(tmp_path):
    """5日外信号仅在最后一根为真：asof 前移一天即不命中。"""
    cfg = _cfg(tmp_path)
    hit = ir.run_weekly_review(
        cfg, asof=ASOF, codes=["SSE.STK.600001"], rule_ids=["txt_先跌后涨新版5日外"],
        bar_loader=_fake_loader, surface_resolver=_ok_surface,
    )
    assert hit["rules"][0]["count"] == 1
    miss = ir.run_weekly_review(
        cfg, asof=20260827, codes=["SSE.STK.600001"], rule_ids=["txt_先跌后涨新版5日外"],
        bar_loader=_fake_loader, surface_resolver=_ok_surface,
    )
    assert miss["asof"] == 20260827
    assert miss["rules"][0]["count"] == 0


def test_review_idempotent_and_force(tmp_path):
    cfg = _cfg(tmp_path)
    ir.run_weekly_review(
        cfg, asof=ASOF, codes=["SSE.STK.600000"],
        bar_loader=_fake_loader, surface_resolver=_ok_surface,
    )

    def _boom(code, asof):
        raise AssertionError("loader must not run when cached")

    cached = ir.run_weekly_review(
        cfg, asof=ASOF, codes=["SSE.STK.600000"],
        bar_loader=_boom, surface_resolver=_ok_surface,
    )
    assert cached.get("reused") is True
    assert cached["status"] == "ok"

    forced = ir.run_weekly_review(
        cfg, asof=ASOF, codes=["SSE.STK.600000"], force=True,
        bar_loader=_fake_loader, surface_resolver=_ok_surface,
    )
    assert "reused" not in forced
    assert forced["rules"][0]["count"] == 1


def test_review_no_go(tmp_path):
    """正式 L1 缺失：status=no_go、matched 置空、不扫描。"""
    cfg = _cfg(tmp_path)

    def _bad_surface(_cfg):
        return None, "no_formal_l1_product"

    summary = ir.run_weekly_review(
        cfg, asof=ASOF, codes=["SSE.STK.600000"],
        bar_loader=_fake_loader, surface_resolver=_bad_surface,
    )
    assert summary["status"] == "no_go"
    assert summary["no_go_reason"] == "no_formal_l1_product"
    assert summary["scanned"] == 0
    assert all(r["count"] == 0 and r["matched"] == [] for r in summary["rules"])
    assert ir.review_output_path(cfg, ASOF).exists()


def test_review_universe_missing_raises(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    # 隔离外部环境：防止开发者机器 MARKET_DATA_ROOT 指向真实仓库导致
    # 数据根兜底成功、断言失真
    monkeypatch.setenv("MARKET_DATA_ROOT", str(tmp_path / "md_absent"))
    with pytest.raises(FileNotFoundError):
        ir.run_weekly_review(
            cfg, asof=ASOF, bar_loader=_fake_loader, surface_resolver=_ok_surface,
        )


def test_review_universe_json_used(tmp_path):
    """codes=None 且 universe.json 存在：票池取文件内容（与导出同源优先项）。"""
    from wtpy.apps.astock.data.universe import AShareUniverse, SymbolInfo

    cfg = _cfg(tmp_path)
    uni = AShareUniverse(
        [
            SymbolInfo(raw="sh600000", std_code="SSE.STK.600000",
                       exchange="SSE", code="600000"),
            SymbolInfo(raw="sh600002", std_code="SSE.STK.600002",
                       exchange="SSE", code="600002"),
        ]
    )
    uni.save(cfg.universe_path)
    summary = ir.run_weekly_review(
        cfg, asof=ASOF, bar_loader=_fake_loader, surface_resolver=_ok_surface,
    )
    assert summary["universe_size"] == 2
    assert summary["rules"][0]["count"] == 1  # 600000 命中 735


def test_no_go_preserves_existing_ok(tmp_path):
    """已有 ok 结果时，无 force 的 no_go 重跑不得覆盖（导出侧保 sheet）。"""
    cfg = _cfg(tmp_path)
    ir.run_weekly_review(
        cfg, asof=ASOF, codes=["SSE.STK.600000"],
        bar_loader=_fake_loader, surface_resolver=_ok_surface,
    )

    def _bad_surface(_cfg):
        return None, "pair_pointer_transiently_gone"

    out = ir.run_weekly_review(
        cfg, asof=ASOF, codes=["SSE.STK.600000"],
        bar_loader=_fake_loader, surface_resolver=_bad_surface,
    )
    assert out["status"] == "ok" and out.get("reused") is True
    on_disk = json.loads(
        ir.review_output_path(cfg, ASOF).read_text(encoding="utf-8")
    )
    assert on_disk["status"] == "ok"

    # force 显式重算时 no_go 才允许覆盖
    forced = ir.run_weekly_review(
        cfg, asof=ASOF, codes=["SSE.STK.600000"], force=True,
        bar_loader=_fake_loader, surface_resolver=_bad_surface,
    )
    assert forced["status"] == "no_go"
    on_disk = json.loads(
        ir.review_output_path(cfg, ASOF).read_text(encoding="utf-8")
    )
    assert on_disk["status"] == "no_go"


def test_review_error_recorded(tmp_path):
    """加载失败的票计入 error_count 且不中断扫描。"""
    cfg = _cfg(tmp_path)
    summary = ir.run_weekly_review(
        cfg, asof=ASOF, codes=["SSE.STK.600000", "SSE.STK.999999"],
        bar_loader=_fake_loader, surface_resolver=_ok_surface,
    )
    assert summary["error_count"] == 1
    assert summary["errors"][0]["code"] == "SSE.STK.999999"
    assert summary["rules"][0]["count"] == 1  # 好票照常命中


def test_review_progress_callback(tmp_path):
    cfg = _cfg(tmp_path)
    seen = []
    ir.run_weekly_review(
        cfg, asof=ASOF, codes=list(BARS_BY_CODE),
        bar_loader=_fake_loader, surface_resolver=_ok_surface,
        on_progress=lambda info: seen.append(info),
    )
    assert seen and seen[-1]["done"] == 4 and seen[-1]["total"] == 4


def test_load_review_for_export_exact_and_fallback(tmp_path):
    cfg = _cfg(tmp_path)
    # 无文件
    review, note = ir.load_review_for_export(cfg, ASOF)
    assert review is None and note.startswith("missing")
    # 精确命中
    ir._atomic_write_json(
        ir.review_output_path(cfg, ASOF),
        {"asof": ASOF, "status": "ok", "rules": []},
    )
    review, note = ir.load_review_for_export(cfg, ASOF)
    assert review is not None and note == ""
    # 回看：导出日无文件，用 3 天前的复核（周末导出场景）
    review, note = ir.load_review_for_export(cfg, 20260831)
    assert review is not None and note.startswith("fallback")
    # 超龄期视为过期
    review, note = ir.load_review_for_export(cfg, 20260930)
    assert review is None and note.startswith("stale")
    # no_go 原样返回并带原因
    ir._atomic_write_json(
        ir.review_output_path(cfg, 20260904),
        {"asof": 20260904, "status": "no_go", "no_go_reason": "x", "rules": []},
    )
    review, note = ir.load_review_for_export(cfg, 20260904)
    assert review["status"] == "no_go" and note.startswith("no_go")


def test_cli_review_weekly_smoke(tmp_path, monkeypatch, capsys):
    """CLI 冒烟：参数透传 + stdout JSON 摘要。"""
    from wtpy.apps.astock import cli

    captured = {}

    def _fake_run(cfg, asof=None, **kw):
        captured["asof"] = asof
        captured.update(kw)
        return {"asof": asof or 20260828, "status": "ok", "rules": []}

    monkeypatch.setattr(ir, "run_weekly_review", _fake_run)
    rc = cli.main(
        ["review-weekly", "--asof", str(ASOF), "--codes", "600000", "--force"]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert captured["asof"] == ASOF
    assert captured["codes"] == "600000"
    assert captured["force"] is True
