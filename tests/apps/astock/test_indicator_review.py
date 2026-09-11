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


def test_review_persist_false_never_touches_disk(tmp_path):
    """persist=False（导出侧即时计算）：不读缓存、不落盘——
    已有缓存时也强制重算，且结果只返回不写文件（防污染周五链产物）。"""
    cfg = _cfg(tmp_path)
    # 先用 persist=True 落一份周五链产物
    first = ir.run_weekly_review(
        cfg, asof=ASOF, codes=["SSE.STK.600000"],
        bar_loader=_fake_loader, surface_resolver=_ok_surface,
    )
    assert first["status"] == "ok"
    assert ir.review_output_path(cfg, ASOF).exists()

    # persist=False：即使已有缓存也重算（loader 必须被调用）且不写盘
    before = ir.review_output_path(cfg, ASOF).read_text(encoding="utf-8")

    def _strict_loader(code, asof):
        raise AssertionError("persist=False must not read cache")

    # 缓存存在 → persist=False 不复用缓存，走真 loader
    calls = {"n": 0}

    def _counting_loader(code, asof):
        calls["n"] += 1
        return _fake_loader(code, asof)

    out = ir.run_weekly_review(
        cfg, asof=ASOF, codes=["SSE.STK.600000"], persist=False,
        bar_loader=_counting_loader, surface_resolver=_ok_surface,
    )
    assert out["status"] == "ok"
    assert "reused" not in out
    assert calls["n"] == 1  # 真实扫描而非读缓存
    assert out["rules"][0]["count"] == 1
    # 磁盘文件原样（未被覆盖、未新增）
    after = ir.review_output_path(cfg, ASOF).read_text(encoding="utf-8")
    assert after == before


def test_review_persist_false_writes_nothing_when_absent(tmp_path):
    """persist=False 在无缓存时也不产生任何文件。"""
    cfg = _cfg(tmp_path)
    out = ir.run_weekly_review(
        cfg, asof=ASOF, codes=["SSE.STK.600000"], persist=False,
        bar_loader=_fake_loader, surface_resolver=_ok_surface,
    )
    assert out["status"] == "ok"
    assert not ir.review_output_path(cfg, ASOF).exists()
    assert not (cfg.storage_root / "indicator_review").exists() or not any(
        (cfg.storage_root / "indicator_review").glob("review_*.json")
    )


def test_review_no_go_persist_false_no_write(tmp_path):
    """no_go + persist=False：不落 no_go 文件。"""
    cfg = _cfg(tmp_path)

    def _bad_surface(_cfg):
        return None, "no_formal_l1_product"

    out = ir.run_weekly_review(
        cfg, asof=ASOF, codes=["SSE.STK.600000"], persist=False,
        bar_loader=_fake_loader, surface_resolver=_bad_surface,
    )
    assert out["status"] == "no_go"
    assert not ir.review_output_path(cfg, ASOF).exists()


def test_sanitize_sheet_name():
    """非法字符替换、31 字符截断、空名/保留字回退 rule_id。"""
    assert ir._sanitize_sheet_name("735金叉及趋势", "txt_735金叉及趋势") == "735金叉及趋势"
    assert ir._sanitize_sheet_name("a[b]:*?/\\c", "rid") == "a_b______c"
    long = "字" * 40
    assert len(ir._sanitize_sheet_name(long, "rid")) == 31
    assert ir._sanitize_sheet_name("", "rid") == "rid"
    assert ir._sanitize_sheet_name("meta", "rid") == "rid"
    assert ir._sanitize_sheet_name("stock-all", "rid") == "rid"
    assert ir._sanitize_sheet_name("etf-all", "rid") == "rid"


def test_sanitize_sheet_name_controls_reserved_and_formula():
    """控制字符/首尾引号/Excel 保留名/公式起始字符都被安全化。"""
    assert ir._sanitize_sheet_name("A\x01B", "rid") == "A_B"
    assert ir._sanitize_sheet_name("A\x7fB", "rid") == "A_B"
    assert ir._sanitize_sheet_name("'引号'", "rid") == "引号"
    assert ir._sanitize_sheet_name("History", "rid") == "rid"
    assert ir._sanitize_sheet_name("history", "rid") == "rid"
    for lead in ("=1+1", "+1", "-1", "@x"):
        cleaned = ir._sanitize_sheet_name(lead, "rid")
        assert cleaned[:1] not in ("=", "+", "-", "@"), cleaned
        assert 0 < len(cleaned) <= 31
    assert ir._sanitize_sheet_name("=1+1", "rid") == "_1+1"
    # 普通名字不受影响
    assert ir._sanitize_sheet_name("常规规则A", "rid") == "常规规则A"


def test_sanitize_sheet_name_final_fallback_never_reserved_or_illegal():
    """rule_id 本身为空/保留名/非法时，最终回退固定安全名，任何输入都不抛。"""
    for rid in ("meta", "History", "stock-all", "etf-all", "", "\x01\x7f", "=" * 5):
        name = ir._sanitize_sheet_name("meta", rid)
        assert 0 < len(name) <= 31, (rid, name)
        assert name.lower() not in {"meta", "stock-all", "etf-all", "history"}
        assert name[:1] not in ("=", "+", "-", "@")
        assert not (set(name) & set("[]:*?/\\"))
        assert all(ord(ch) >= 32 and ord(ch) != 0x7F for ch in name)
    # 正常显示名优先于 rule_id
    assert ir._sanitize_sheet_name("正常名", "meta") == "正常名"


class _FakeProductPair:
    """cutoff 超前（derive 到请求日/今天）但 l1_max_date 是真实行情最后日的产品对。"""

    def __init__(self, *, l1_max_date: int, cutoff: int):
        self.l1_dataset_id = "ds_fake_l1"
        self.l1_max_date = l1_max_date
        self.cutoff = cutoff


def _fake_pair_modules(monkeypatch, *, l1_max_date: int, cutoff: int):
    import wtpy.apps.astock.data.tushare_product as tp

    monkeypatch.setattr(
        tp,
        "resolve_active_tushare_product_pair",
        lambda store, *, deep_copy=True: _FakeProductPair(
            l1_max_date=l1_max_date, cutoff=cutoff
        ),
    )


def test_resolve_formal_surface_prefers_l1_max_date_over_cutoff(tmp_path, monkeypatch):
    """cutoff 超前时 max_date 必须取 l1_max_date（真实行情最后日）。"""
    md = tmp_path / "md"
    md.mkdir()
    monkeypatch.setenv("MARKET_DATA_ROOT", str(md))
    cfg = _cfg(tmp_path)
    _fake_pair_modules(monkeypatch, l1_max_date=ASOF, cutoff=20260910)

    surface, reason = ir._resolve_formal_surface(cfg)
    assert reason == ""
    assert surface is not None
    assert surface["max_date"] == ASOF


def test_run_weekly_review_uses_l1_max_date_not_cutoff(tmp_path, monkeypatch):
    """请求日=cutoff（20260910）但 L1 行情止于 ASOF：复核应落在 ASOF 并命中。"""
    md = tmp_path / "md"
    md.mkdir()
    monkeypatch.setenv("MARKET_DATA_ROOT", str(md))
    cfg = _cfg(tmp_path)
    _fake_pair_modules(monkeypatch, l1_max_date=ASOF, cutoff=20260910)

    summary = ir.run_weekly_review(
        cfg, asof=20260910, codes=["SSE.STK.600000"], bar_loader=_fake_loader,
    )
    assert summary["status"] == "ok"
    assert summary["asof"] == ASOF
    assert summary["rules"][0]["count"] == 1


def test_resolve_review_asof_falls_back_beyond_data(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        ir,
        "_resolve_formal_surface",
        lambda _cfg: ({"formal_l1_id": "x", "max_date": ASOF}, ""),
    )
    eff, note = ir.resolve_review_asof(cfg, 20260910)
    assert eff == ASOF
    assert note.startswith("fallback_date:请求 20260910 超出数据覆盖 20260828")
    assert f"信号按 {ASOF} 计算" in note


def test_resolve_review_asof_keeps_request_within_data(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        ir,
        "_resolve_formal_surface",
        lambda _cfg: ({"formal_l1_id": "x", "max_date": ASOF}, ""),
    )
    eff, note = ir.resolve_review_asof(cfg, 20260820)
    assert eff == 20260820
    assert note == ""


def test_resolve_review_asof_without_surface_keeps_request(tmp_path, monkeypatch):
    """无正式 L1：原样返回请求日与空 note（调用方走 no_go/即时计算）。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        ir,
        "_resolve_formal_surface",
        lambda _cfg: (None, "no_formal_l1_product"),
    )
    eff, note = ir.resolve_review_asof(cfg, 20260910)
    assert eff == 20260910
    assert note == ""


def test_review_explicit_default_rule_ids_keep_short_sheet_names(tmp_path):
    """显式传默认两条规则 ID：sheet 名仍取 DEFAULT_REVIEW_RULES 短名，
    不得退化成完整 rule_id（与预计算路径同工作簿 sheet 名一致）。"""
    cfg = _cfg(tmp_path)
    out = ir.run_weekly_review(
        cfg, asof=ASOF, codes=list(BARS_BY_CODE),
        rule_ids=[rid for rid, _s in ir.DEFAULT_REVIEW_RULES],
        persist=False,
        bar_loader=_fake_loader, surface_resolver=_ok_surface,
    )
    assert out["status"] == "ok"
    assert [(r["rule_id"], r["sheet"]) for r in out["rules"]] == [
        ("txt_735金叉及趋势", "735"),
        ("txt_先跌后涨新版5日外", "5日外"),
    ]
    assert out["rules"][0]["count"] == 1
    assert out["rules"][1]["count"] == 1


def test_review_custom_rule_sheet_uses_display_name(tmp_path):
    """非默认规则（用户规则）sheet 名=显示名 sanitize，而非 rule_id。"""
    cfg = _cfg(tmp_path)
    from wtpy.apps.astock.indicators.registry import IndicatorRegistry

    # 注册一条用户规则（显示名含非法字符）到 指标/ 目录同源注册表
    # 直接走 registry bootstrap 需要公式文件；这里用 monkeypatch 替换 bootstrap
    class _FakeSpec:
        id = "user_demo"
        name = "我的[规则]A"
        compile_status = "ready"
        failure_reason = None

    def _fake_bootstrap(indicator_dir, mapping_path=None, **kwargs):
        class _FakeReg:
            def get(self, rid):
                if rid == "user_demo":
                    return _FakeSpec()
                raise KeyError(rid)
        return _FakeReg()

    import wtpy.apps.astock.indicators.registry as reg_mod

    orig = reg_mod.IndicatorRegistry.bootstrap
    reg_mod.IndicatorRegistry.bootstrap = staticmethod(_fake_bootstrap)
    try:
        out = ir.run_weekly_review(
            cfg, asof=ASOF, codes=["SSE.STK.600000"], rule_ids=["user_demo"],
            persist=False,
            bar_loader=_fake_loader, surface_resolver=_ok_surface,
        )
        assert out["status"] == "ok"
        assert out["rules"][0]["rule_id"] == "user_demo"
        assert out["rules"][0]["sheet"] == "我的_规则_A"
    finally:
        reg_mod.IndicatorRegistry.bootstrap = orig


def test_review_user_rule_resolved_from_user_registry(tmp_path):
    """用户规则只在 storage_root/indicators/user_registry.json：复核热路径的
    bootstrap 必须合并该文件（只读），否则 reg.get(user_*) 抛 KeyError、
    整体即时计算失败、导出漏 sheet。"""
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.service.rules import RuleService

    cfg = get_default_config(
        storage_root=tmp_path, indicator_dir=tmp_path / "empty_ind"
    )
    svc = RuleService(cfg)
    created = svc.create_rule(name="趋势回踩低吸", formula_text="XG:C>0;")
    assert created["id"].startswith("user_")
    assert (tmp_path / "indicators" / "user_registry.json").exists()

    out = ir.run_weekly_review(
        cfg, asof=ASOF, codes=["SSE.STK.600000"], rule_ids=[created["id"]],
        persist=False,
        bar_loader=_fake_loader, surface_resolver=_ok_surface,
    )
    assert out["status"] == "ok"
    assert out["rules"][0]["rule_id"] == created["id"]
    assert out["rules"][0]["sheet"] == "趋势回踩低吸"
    assert out["rules"][0]["count"] == 1


def test_resolve_rule_sheet_names_fallback_chain(tmp_path):
    """占位 sheet 名解析：默认短名 > 用户注册表显示名 > rule_id。"""
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.indicators.models import IndicatorSpec
    from wtpy.apps.astock.indicators.registry import IndicatorRegistry

    cfg = get_default_config(
        storage_root=tmp_path, indicator_dir=tmp_path / "empty_ind"
    )
    upath = tmp_path / "indicators" / "user_registry.json"
    upath.parent.mkdir(parents=True, exist_ok=True)
    IndicatorRegistry(
        [
            IndicatorSpec(
                id="user_demo",
                name="我的[规则]",
                version="user:x",
                kind="tdx_formula",
                output_type="signal",
                supported_periods=("DAY",),
                compile_status="ready",
                formula_text="XG:C>0;",
                dependencies=[],
            )
        ]
    ).save(upath)

    names = ir.resolve_rule_sheet_names(
        cfg, ["txt_735金叉及趋势", "user_demo", "user_missing"]
    )
    assert names["txt_735金叉及趋势"] == "735"
    assert names["user_demo"] == "我的_规则_"
    assert names["user_missing"] == "user_missing"


def test_resolve_rule_sheet_names_system_registry_branch(tmp_path):
    """用户注册表缺失时占位名回退系统注册表（指标目录 .txt 显示名）。"""
    from wtpy.apps.astock.config import get_default_config

    ind = tmp_path / "ind"
    ind.mkdir()
    (ind / "系统规则.txt").write_text("XG:C>0;", encoding="utf-8")
    cfg = get_default_config(
        storage_root=tmp_path / "st", indicator_dir=ind
    )
    names = ir.resolve_rule_sheet_names(cfg, ["txt_系统规则", "user_missing"])
    assert names["txt_系统规则"] == "系统规则"
    assert names["user_missing"] == "user_missing"


def test_review_no_go_explicit_default_ids_keep_short_sheet_names(tmp_path):
    """no_go 摘要中显式回传的默认规则 ID 也映射回短 sheet 名（与 ok 一致）。"""
    cfg = _cfg(tmp_path)

    def _bad_surface(_cfg):
        return None, "no_formal_l1_product"

    out = ir.run_weekly_review(
        cfg,
        asof=ASOF,
        codes=["SSE.STK.600000"],
        rule_ids=[rid for rid, _s in ir.DEFAULT_REVIEW_RULES],
        persist=False,
        surface_resolver=_bad_surface,
    )
    assert out["status"] == "no_go"
    assert [(r["rule_id"], r["sheet"]) for r in out["rules"]] == [
        ("txt_735金叉及趋势", "735"),
        ("txt_先跌后涨新版5日外", "5日外"),
    ]

    custom = ir.run_weekly_review(
        cfg,
        asof=ASOF,
        codes=["SSE.STK.600000"],
        rule_ids=["user_custom"],
        persist=False,
        surface_resolver=_bad_surface,
    )
    assert custom["rules"][0]["sheet"] == "user_custom"


def test_review_user_rule_formula_like_name_sheet_sanitized(tmp_path):
    """规则名形如公式/含控制字符：sheet 名被安全化，复核不抛异常。"""
    from wtpy.apps.astock.service.rules import RuleService

    cfg = get_default_config(
        storage_root=tmp_path, indicator_dir=tmp_path / "empty_ind"
    )
    svc = RuleService(cfg)
    eq = svc.create_rule(name="=1+1", formula_text="XG:C>0;")
    ctrl = svc.create_rule(name="A\x01B", formula_text="XG:C>0;")

    out = ir.run_weekly_review(
        cfg,
        asof=ASOF,
        codes=["SSE.STK.600000"],
        rule_ids=[eq["id"], ctrl["id"]],
        persist=False,
        bar_loader=_fake_loader,
        surface_resolver=_ok_surface,
    )
    assert out["status"] == "ok"
    sheets = {r["rule_id"]: r["sheet"] for r in out["rules"]}
    assert sheets[eq["id"]] == "_1+1"
    assert sheets[ctrl["id"]] == "A_B"
