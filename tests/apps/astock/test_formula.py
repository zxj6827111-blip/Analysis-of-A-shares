"""Formula parser / runtime tests."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401
import numpy as np
import pytest

from wtpy.apps.astock.indicators.builtins import fn_barslast, fn_count, fn_cross, fn_ema, fn_ma, fn_ref
from wtpy.apps.astock.indicators.compiler import compile_formula
from wtpy.apps.astock.indicators.parser import parse_formula
from wtpy.apps.astock.indicators.runtime import run_formula


def test_parse_v5_like():
    src = """
MA5:=MA(C,5);
DIF:=EMA(C,12)-EMA(C,26);
DEA:=EMA(DIF,9);
MC:=CROSS(DIF,DEA);
XG:MA5>0 AND MC;
"""
    prog = parse_formula(src)
    assert any(s.name == "XG" and s.output for s in prog.statements)
    cr = compile_formula(src, indicator_id="t")
    assert cr.ok, cr.error


def test_unknown_function_reports_name():
    src = "A:=FOOBAR(C,1);\nXG:A;"
    cr = compile_formula(src, indicator_id="demo")
    assert not cr.ok
    assert "FOOBAR" in (cr.error or "")
    assert "demo" in (cr.error or "")


def test_ma_ref_cross_semantics():
    c = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=float)
    ma3 = fn_ma(c, 3)
    assert np.isnan(ma3[1])
    assert ma3[2] == pytest.approx(2.0)
    ref = fn_ref(c, 1)
    assert np.isnan(ref[0])
    assert ref[1] == 1
    a = np.array([1, 1, 3, 3], float)
    b = np.array([2, 2, 2, 2], float)
    cross = fn_cross(a, b)
    assert cross[0] == 0
    assert cross[2] == 1  # 1<=2 then 3>2


def test_barslast_count():
    cond = np.array([0, 1, 0, 0, 1, 0], float)
    bl = fn_barslast(cond)
    assert np.isnan(bl[0])
    assert bl[1] == 0
    assert bl[3] == 2
    assert bl[4] == 0
    ct = fn_count(cond, 3)
    # window [2,3,4] values 0,0,1 -> count 1 (0-based index 4)
    assert ct[4] == 1
    assert np.isnan(ct[1])  # pre-warm


def test_runtime_xg_no_eval():
    src = """
MA5:=MA(C,5);
XG:C>MA5;
"""
    bars = {
        "close": np.array([1, 2, 3, 4, 5, 6, 7], float),
        "open": np.array([1, 2, 3, 4, 5, 6, 7], float),
        "high": np.array([1, 2, 3, 4, 5, 6, 7], float),
        "low": np.array([1, 2, 3, 4, 5, 6, 7], float),
        "volume": np.ones(7),
    }
    res = run_formula(src, bars, indicator_id="x")
    assert res.error is None
    assert res.signal is not None
    # MA5 at index4 = mean(1..5)=3; C=5 > 3 -> True
    assert int(res.signal[4]) == 1
    # index0 C=1, MA5 nan/not ready -> signal false/0
    assert int(res.signal[0]) == 0


def test_min60_cross_ref_blocks():
    src = """
DIF60:="MACD.DIF#MIN60";
XG:DIF60>0;
"""
    bars = {
        "close": np.ones(10),
        "open": np.ones(10),
        "high": np.ones(10),
        "low": np.ones(10),
        "volume": np.ones(10),
    }
    res = run_formula(src, bars, indicator_id="m60", allow_missing_cross=False)
    assert res.error is not None
    assert "MIN60" in res.error or "cross-period" in res.error.lower()


def test_real_v5_formula_compiles():
    from pathlib import Path

    p = Path(__file__).resolve().parents[3] / "指标" / "多周期共振选股V5.txt"
    if not p.exists():
        pytest.skip("V5 txt missing")
    src = p.read_text(encoding="utf-8")
    cr = compile_formula(src, indicator_id="v5")
    assert cr.ok, cr.error
    bars = {
        "close": np.linspace(10, 20, 80),
        "open": np.linspace(10, 20, 80),
        "high": np.linspace(10.5, 20.5, 80),
        "low": np.linspace(9.5, 19.5, 80),
        "volume": np.ones(80) * 1000,
    }
    res = run_formula(src, bars, indicator_id="v5")
    assert res.error is None
    assert res.signal is not None
    assert len(res.signal) == 80
