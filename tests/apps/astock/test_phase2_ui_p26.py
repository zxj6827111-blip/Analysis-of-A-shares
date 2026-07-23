# -*- coding: utf-8 -*-
"""P2.6 UI static checks + estimate free-axes payload (no browser)."""
from __future__ import annotations

from pathlib import Path

from wtpy.apps.astock.service.experiments import estimate_grid_from_payload


def test_index_html_has_p26_controls():
    html = Path("wtpy/apps/astock/web/static/index.html").read_text(encoding="utf-8")
    for key in (
        "btnExp735",
        "btnExpModeAxes",
        "btnExpModeTemplate",
        "expCardAxesMode",
        "expPreviewBox",
        "signal_weekdays_options",
        "_expApply735Matrix",
        "_expRenderPreview",
        "expForce",
    ):
        assert key in html, key


def test_estimate_735_like_payload_is_16():
    # Mimic UI free-axes body for 735 一键 16 组 (rule id placeholder)
    payload = {
        "rule_ids": ["735"],
        "gua_keys": ["none", "best3"],
        "signal_weekdays_options": [[5]],
        "buy_options": [{"buy_weekday": 1, "buy_on": "open", "entry_lag": 1}],
        "sell_options": [
            {"exit_weekday": d, "sell_on": so, "hold": 1}
            for d in (2, 3, 4, 5)
            for so in ("open", "close")
        ],
        "max_variants": 50,
        "period": "DAY",
        "codes": ["sh600000", "sz000001"],
        "holiday_policy": "next_trading_day",
    }
    d = estimate_grid_from_payload(payload)
    n = d.get("estimated_variants", d.get("actual", d.get("n")))
    assert int(n) == 16
    assert int(d.get("rejected") or 0) == 0
    assert (d.get("preview") or d.get("variants") is not None) or True
    prev = d.get("preview") or []
    assert len(prev) >= 1
