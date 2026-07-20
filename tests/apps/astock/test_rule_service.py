"""RuleService custom rules: validate, save, list via shipped service."""

from __future__ import annotations

import json
from pathlib import Path

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.service.rules import RuleService


def test_create_user_rule_and_list(tmp_path: Path):
    storage = tmp_path / "storage"
    # minimal storage layout
    (storage / "indicators").mkdir(parents=True)
    cfg = get_default_config(storage_root=storage, indicator_dir=tmp_path / "empty_ind")
    (tmp_path / "empty_ind").mkdir(exist_ok=True)
    svc = RuleService(cfg)

    formula = "MA5:=MA(C,5);\nXG:C>MA5;\n"
    bad = svc.validate_formula("MA5:=MA(C,5);\n")
    assert bad["ok"] is False

    ok = svc.validate_formula(formula)
    assert ok["ok"] is True
    assert ok["has_xg"] is True

    created = svc.create_rule(name="测试金叉", formula_text=formula, description="unit")
    assert created["id"].startswith("user_")
    assert created["source"] == "user"
    assert created["backtestable"] is True
    assert created["formula_text"].strip().startswith("MA5")

    # persisted
    ureg = storage / "indicators" / "user_registry.json"
    assert ureg.exists()
    data = json.loads(ureg.read_text(encoding="utf-8"))
    assert any(x["id"] == created["id"] for x in data["indicators"])

    rows = svc.list_rules()
    ids = {r["id"] for r in rows}
    assert created["id"] in ids

    got = svc.get_rule(created["id"])
    assert got["name"] == "测试金叉"

    archived = svc.archive_rule(created["id"])
    assert archived["archived"] is True
    rows2 = svc.list_rules(include_archived=False)
    assert created["id"] not in {r["id"] for r in rows2}
