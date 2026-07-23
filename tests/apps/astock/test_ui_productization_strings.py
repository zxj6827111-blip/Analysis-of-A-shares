# -*- coding: utf-8 -*-
"""Lightweight productization string checks for experiment UI (Task C)."""

from pathlib import Path

INDEX = (
    Path(__file__).resolve().parents[3]
    / "wtpy"
    / "apps"
    / "astock"
    / "web"
    / "static"
    / "index.html"
)


def test_ui_productization_strings_present():
    text = INDEX.read_text(encoding="utf-8")
    required = [
        "stop_loss_list",
        "take_profit_list",
        "expEvalPanel",
        "research/evaluate",
        "btnExpEvaluate",
        "expSlChips",
        "expTpChips",
        "expRunEvaluate",
        "expResearchHub",
        "research/queue",
        "workers/reclaim",
    ]
    missing = [s for s in required if s not in text]
    assert not missing, f"index.html missing productization markers: {missing}"


def test_ui_productization_eval_panes():
    text = INDEX.read_text(encoding="utf-8")
    for pane in (
        "expEvalPane-overview",
        "expEvalPane-rank",
        "expEvalPane-heat",
        "expEvalPane-gua",
        "expEvalPane-yearly",
        "expEvalPane-stab",
    ):
        assert pane in text, pane


def test_exp_inline_rule_picker_markers():
    from pathlib import Path
    html = Path("wtpy/apps/astock/web/static/index.html").read_text(encoding="utf-8")
    for key in ("expRulePicker", "renderExpRulePicker", "btnExpClearRules", "exp-rule-picker"):
        assert key in html, key
    assert 'name === "history" || name === "detail" || name === "experiment"' not in html
