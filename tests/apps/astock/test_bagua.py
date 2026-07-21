"""Bagua knowledge and fixed sample tests."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401
from pathlib import Path

import pytest

from wtpy.apps.astock.bagua.calculator import (
    BaguaCalculator,
    BaguaKnowledge,
    digit_sum_price,
    format_price_2,
    mod_map,
)


JSON_PATH = (
    Path(__file__).resolve().parents[3]
    / "wtpy"
    / "apps"
    / "astock"
    / "bagua"
    / "bagua_384.json"
)


@pytest.fixture(scope="module")
def calc():
    assert JSON_PATH.exists()
    return BaguaCalculator.from_json(JSON_PATH)


def test_knowledge_64x6(calc):
    issues = calc.knowledge.validate()
    assert issues == []


def test_price_format_keeps_zero():
    assert format_price_2(5.9) == "5.90"
    assert format_price_2("5.90") == "5.90"
    assert digit_sum_price(5.90) == 5 + 9 + 0
    assert digit_sum_price(6.27) == 6 + 2 + 7
    assert digit_sum_price(7.33) == 7 + 3 + 3


def test_mod_map_zero():
    assert mod_map(16, 8) == 8
    assert mod_map(12, 6) == 6


def test_fixed_sample_shanshuimeng(calc):
    r = calc.calculate(open_price=6.27, high_price=7.33, low_price=5.90, close_price=5.90)
    assert r.open_digit_sum == 15
    assert r.close_digit_sum == 14
    assert r.upper_id == 7
    assert r.upper_name == "艮"
    assert r.upper_alias == "山"
    assert r.upper_symbol == "☶"
    assert r.lower_id == 6
    assert r.lower_name == "坎"
    assert r.lower_alias == "水"
    assert r.lower_symbol == "☵"
    assert r.yao_order == 3
    assert r.yao_name == "六三"
    assert "山水蒙" in r.full_name or r.gua_name == "山水蒙"
    assert r.gua_symbol == "䷃"
    assert "诱多陷阱不可追涨" in r.market_judgement


def test_excel_row_consistency(calc):
    from pathlib import Path
    ind_dir = Path(__file__).resolve().parents[3] / '指标'
    preferred = list(ind_dir.glob('*操作信号*.xlsx'))
    xlsxs = preferred or list(ind_dir.glob('*.xlsx'))
    if not xlsxs:
        import pytest
        pytest.skip('excel missing')
    xlsx = xlsxs[0]
    issues = calc.knowledge.excel_consistency_check(xlsx)
    assert issues == [], issues[:5]


def test_gua_order_1_to_64_six_each(calc):
    from collections import Counter
    c = Counter(int(e["gua_order"]) for e in calc.knowledge.entries)
    assert set(c) == set(range(1, 65))
    assert all(v == 6 for v in c.values())
    # core_gang present on all
    assert all(e.get("core_gang") for e in calc.knowledge.entries)
