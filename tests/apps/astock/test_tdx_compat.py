# -*- coding: utf-8 -*-
"""通达信原生兼容改造（单引号/SMA/NAMELIKE/DYNAINFO）引擎层测试。

全部 CI-safe：内联公式文本 + 合成 numpy 数据，不依赖 指标/ 目录。
固定验收样本 = 用户「短线强势启动」完整公式原文（含去风险行）。
"""

from __future__ import annotations

import numpy as np
import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.indicators.builtins import fn_ema, fn_sma
from wtpy.apps.astock.indicators.compiler import compile_formula
from wtpy.apps.astock.indicators.parser import parse_formula
from wtpy.apps.astock.indicators.runtime import run_formula

# 用户四套公式原文（未删去风险行的原始粘贴形态）
RISK = "去风险:=NOT(NAMELIKE('ST') OR NAMELIKE('*ST') OR DYNAINFO(4)=0);\n"
FORMULA_STARTUP = (
    "{短线强势启动 盘后选股 无未来}\n"
    "MA5:=MA(C,5);\n"
    "MA10:=MA(C,10);\n"
    "MA20:=MA(C,20);\n"
    "均线多头:=MA5>MA10 AND MA10>MA20 AND C>MA5;\n"
    "放量:=V>MA(V,5)*1.2;\n"
    "DIF:=EMA(C,12)-EMA(C,26);\n"
    "DEA:=EMA(DIF,9);\n"
    "MACD金叉:=CROSS(DIF,DEA) AND DIF>0;\n"
    + RISK
    + "XG:均线多头 AND 放量 AND MACD金叉 AND 去风险;\n"
)
FORMULA_PULLBACK = (
    "{趋势回踩低吸 盘后 无未来}\n"
    "MA5:=MA(C,5);\n"
    "MA10:=MA(C,10);\n"
    "MA20:=MA(C,20);\n"
    "趋势向上:=MA20>REF(MA20,1);\n"
    "回踩:=L<=MA10 AND C>MA10;\n"
    "缩量:=V<MA(V,5);\n"
    + RISK
    + "XG:趋势向上 AND 回踩 AND 缩量 AND 去风险;\n"
)
FORMULA_BREAKOUT = (
    "{20日平台突破 盘后 无未来}\n"
    "HH:=HHV(H,20);\n"
    "平台突破:=CROSS(C,REF(HH,1));\n"
    "放量:=V>REF(V,1)*1.4;\n"
    "MA20:=MA(C,20);\n"
    "趋势:=MA20>REF(MA20,1);\n"
    + RISK
    + "XG:平台突破 AND 放量 AND 趋势 AND 去风险;\n"
)
FORMULA_RSI = (
    "{短线超跌企稳 盘后 无未来}\n"
    "MA5:=MA(C,5);\n"
    "RSI:=SMA(MAX(C-REF(C,1),0),6,1)/SMA(ABS(C-REF(C,1)),6,1)*100;\n"
    "超跌:=RSI<30;\n"
    "止跌阳线:=C>REF(C,1) AND C>O;\n"
    "量缩:=V<MA(V,5);\n"
    + RISK
    + "XG:超跌 AND 止跌阳线 AND 量缩 AND 去风险;\n"
)
USER_FORMULAS = [
    FORMULA_STARTUP,
    FORMULA_PULLBACK,
    FORMULA_BREAKOUT,
    FORMULA_RSI,
]


def _synth_bars(n=120, seed=7):
    """合成 K 线：前 15 根平盘（RSI 0/0 压力段）+ 涨-跌-涨三段。"""
    rng = np.random.default_rng(seed)
    close = np.empty(n)
    close[:15] = 10.0
    w = 10.0
    for i in range(15, n):
        drift = 0.004 if (i < 50 or i > 80) else -0.006
        w *= 1.0 + drift + rng.normal(0, 0.012)
        close[i] = w
    openp = np.empty(n)
    openp[0] = 10.0
    for i in range(1, n):
        openp[i] = close[i - 1] * (1 + rng.normal(0, 0.004))
    high = np.maximum(openp, close) * 1.01
    low = np.minimum(openp, close) * 0.99
    volume = np.exp(np.log(1e6) + rng.normal(0, 0.35, n))
    volume[::17] *= 2.2
    return {
        "close": close, "open": openp, "high": high, "low": low, "volume": volume,
    }


def _startup_hit_bars():
    """构造「短线强势启动」去风险前确实命中的行情（随机搜索验证过参数）：
    100 根缓跌（MACD 深绿）→ 25 根反弹（DIF 上穿转正）→ 8 根微回调
    （DIF 回落接近 DEA）→ 5 根再涨（末日水上金叉+均线多头+放量）。"""
    segs = []
    c = 20.0
    for _ in range(100):
        c *= (1 - 0.004)
        segs.append(c)
    for _ in range(25):
        c *= (1 + 0.008)
        segs.append(c)
    for _ in range(8):
        c *= (1 - 0.002)
        segs.append(c)
    for _ in range(5):
        c *= (1 + 0.01)
        segs.append(c)
    close = np.array(segs)
    n = len(close)
    openp = close * 0.998
    high = np.maximum(openp, close) * 1.005
    low = np.minimum(openp, close) * 0.995
    volume = np.full(n, 1e6)
    volume[-1] = 2.5e6
    volume[-2] = 1.6e6
    return {
        "close": close, "open": openp, "high": high, "low": low, "volume": volume,
    }


# ---------------- 单引号 tokenizer ----------------

def test_single_quote_string_parses():
    prog = parse_formula("A:=NAMELIKE('ST');\nXG:A;\n")
    assert any(s.name == "A" for s in prog.statements)


def test_single_quote_cross_period_ref():
    """含 # 的单引号串仍走 CrossPeriodRef 路径（与双引号等价）。"""
    src = 'DIF60:="MACD.DIF#MIN60";\n'
    prog2 = parse_formula(src.replace('"', "'"))
    src1 = parse_formula(src)
    from wtpy.apps.astock.indicators import ast_nodes as A

    def _first_cross(p):
        for s in p.statements:
            if isinstance(s.expr, A.CrossPeriodRef):
                return s.expr
        return None

    c1, c2 = _first_cross(src1), _first_cross(prog2)
    assert c1 is not None and c2 is not None
    assert c1.raw == c2.raw == "MACD.DIF#MIN60"


def test_unclosed_single_quote_errors():
    cr = compile_formula("A:=NAMELIKE('ST);\nXG:A;\n", indicator_id="t")
    assert not cr.ok
    assert "unclosed string" in (cr.error or "")


# ---------------- SMA ----------------

def test_sma_recursion_matches_manual():
    """SMA(X,6,1) 递推 Y'=(X+5Y')/6 首值 X1 —— 手算对照。"""
    x = np.array([6.0, 12.0, 3.0, 9.0, 6.0], dtype=float)
    out = fn_sma(x, 6, 1)
    expect = np.empty(5)
    expect[0] = 6.0
    expect[1] = (12.0 + 5 * 6.0) / 6
    expect[2] = (3.0 + 5 * expect[1]) / 6
    expect[3] = (9.0 + 5 * expect[2]) / 6
    expect[4] = (6.0 + 5 * expect[3]) / 6
    assert np.allclose(out, expect, atol=1e-12)


def test_sma_equivalent_ema():
    """SMA(X,N,1) ≡ EMA(X,2N-1)：权重 α=1/N 一致，首值 X1 一致。"""
    rng = np.random.default_rng(42)
    x = rng.normal(10, 1, 300)
    a = fn_sma(x, 6, 1)
    b = fn_ema(x, 11)
    assert np.max(np.abs(a - b)) < 1e-9


def test_sma_nan_passthrough():
    """NaN 透传前值（与 fn_ema 同构）：NaN 根输出前值，NaN 段后继续递推。"""
    x = np.array([5.0, np.nan, 7.0, np.nan, 8.0], dtype=float)
    out = fn_sma(x, 6, 1)
    assert out[0] == 5.0
    assert out[1] == 5.0  # NaN 透传前值
    expect2 = (7.0 + 5 * 5.0) / 6
    assert abs(out[2] - expect2) < 1e-12
    assert out[3] == expect2
    assert abs(out[4] - (8.0 + 5 * expect2) / 6) < 1e-12


def test_sma_param_validation_negative():
    for bad in ["XG:SMA(C,5,6);", "XG:SMA(C,0,1);", "XG:SMA(C,-1,1);",
                "XG:SMA(C,5,-1);", "XG:SMA(C,5);", "XG:SMA(C,4.5,1);"]:
        cr = compile_formula(bad, indicator_id="t")
        assert not cr.ok, bad
        assert "SMA" in (cr.error or ""), bad


# ---------------- NAMELIKE ----------------

def test_namelike_prefix_semantics():
    bars = _synth_bars()
    src = "XG:NAMELIKE('ST');\n"
    assert int(run_formula(src, bars, indicator_id="t", stock_name="ST黔酒").signal[0]) == 1
    assert int(run_formula(src, bars, indicator_id="t", stock_name="*ST银河").signal[0]) == 0
    # 前缀匹配：含 ST 但不以 ST 开头不命中
    assert int(run_formula(src, bars, indicator_id="t", stock_name="酒ST鬼").signal[0]) == 0
    assert int(run_formula(src, bars, indicator_id="t", stock_name="茅台").signal[0]) == 0


def test_namelike_star_st_removed():
    bars = _startup_hit_bars()
    res = run_formula(FORMULA_STARTUP, bars, indicator_id="t", stock_name="*ST银河")
    assert res.error is None
    assert res.signal is not None and int(np.nansum(res.signal)) == 0


def test_namelike_missing_name_errors_visible():
    bars = _synth_bars()
    res = run_formula(FORMULA_STARTUP, bars, indicator_id="t", stock_name="")
    assert res.error is not None
    assert "NAMELIKE" in res.error
    assert res.signal is None  # 不产生信号（报错可见，非静默放行/剔除）


def test_namelike_param_validation_negative():
    for bad in ["XG:NAMELIKE('ST','X');", "XG:NAMELIKE(C);",
                'XG:NAMELIKE("MACD.DIF#MIN60");']:  # 跨周期引用拒绝
        cr = compile_formula(bad, indicator_id="t")
        assert not cr.ok, bad


# ---------------- DYNAINFO ----------------

def test_dynainfo_maps_ohlc_series():
    """DYNAINFO(4/5/6/7) 返回 O/H/L/C 价格序列（=0 比较后才布尔化）。"""
    bars = _synth_bars(20)
    for field, key in [(4, "open"), (5, "high"), (6, "low"), (7, "close")]:
        res = run_formula(f"D:=DYNAINFO({field});\nXG:D;\n", bars, indicator_id="t")
        assert res.error is None
        d = res.variables["D"]
        assert np.allclose(d, bars[key])


def test_dynainfo_zero_open_filters_suspended():
    """开盘价 0 的停牌票被 DYNAINFO(4)=0 剔除（去风险前确实命中）。"""
    bars = _startup_hit_bars()
    res = run_formula(FORMULA_STARTUP, bars, indicator_id="t", stock_name="贵州茅台")
    assert res.error is None
    assert int(np.nansum(res.signal)) >= 1  # 正常名称+开盘价：保留命中
    suspended = dict(bars)
    suspended["open"] = bars["open"].copy()
    suspended["open"][-1] = 0.0  # 末日停牌（开盘 0）
    res2 = run_formula(FORMULA_STARTUP, suspended, indicator_id="t", stock_name="贵州茅台")
    assert res2.error is None
    assert int(res2.signal[-1]) == 0  # 末日被剔除


def test_dynainfo_unsupported_field_rejected():
    for bad in ["XG:DYNAINFO(3);", "XG:DYNAINFO(8);", "XG:DYNAINFO(4.9);",
                "XG:DYNAINFO(C);", "XG:DYNAINFO();"]:
        cr = compile_formula(bad, indicator_id="t")
        assert not cr.ok, bad
        assert "DYNAINFO" in (cr.error or ""), bad


# ---------------- 四套原始公式全链 ----------------

@pytest.mark.parametrize("formula", USER_FORMULAS)
def test_user_formulas_compile_and_run(formula):
    """用户四套公式原文（含单引号+去风险行）可直接编译+运行。"""
    cr = compile_formula(formula, indicator_id="u")
    assert cr.ok, cr.error
    bars = _synth_bars()
    res = run_formula(formula, bars, indicator_id="u", stock_name="贵州茅台")
    assert res.error is None, res.error
    assert res.signal is not None and len(res.signal) == len(bars["close"])
    valid = res.signal[~np.isnan(res.signal)]
    assert bool(np.all((valid == 0) | (valid == 1)))


def test_startup_kept_for_normal_name():
    """普通名称+开盘正常 → 去风险不误伤，命中保留。"""
    bars = _startup_hit_bars()
    without_risk = FORMULA_STARTUP.replace(RISK, "").replace(
        "XG:均线多头 AND 放量 AND MACD金叉 AND 去风险;", "XG:均线多头 AND 放量 AND MACD金叉;"
    )
    base = run_formula(without_risk, bars, indicator_id="t", stock_name="")
    full = run_formula(FORMULA_STARTUP, bars, indicator_id="t", stock_name="贵州茅台")
    assert full.error is None and base.error is None
    assert int(np.nansum(full.signal)) == int(np.nansum(base.signal)) >= 1
