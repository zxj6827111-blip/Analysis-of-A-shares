# -*- coding: utf-8 -*-
"""V3 experiment rule-picker: structure + shipped open/select/confirm path."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
V3 = ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index_v3.html"
HARNESS = Path(__file__).resolve().parent / "v3_rule_pick_harness.js"


@pytest.fixture(scope="module")
def v3_html() -> str:
    assert V3.is_file(), f"missing {V3}"
    return V3.read_text(encoding="utf-8")


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parent_of: dict[str, str | None] = {}
        self.id_stack: list[str | None] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        eid = attrs_d.get("id")
        parent = self.id_stack[-1] if self.id_stack else None
        if eid:
            self.parent_of[eid] = parent
        if tag.lower() in {
            "input",
            "br",
            "hr",
            "img",
            "meta",
            "link",
            "area",
            "base",
            "col",
            "embed",
            "source",
            "track",
            "wbr",
        }:
            return
        self.stack.append(tag)
        self.id_stack.append(
            eid if eid else (self.id_stack[-1] if self.id_stack else None)
        )

    def handle_endtag(self, tag):
        while self.stack:
            t = self.stack.pop()
            if self.id_stack:
                self.id_stack.pop()
            if t.lower() == tag.lower():
                break


def test_v3_rule_pick_modal_not_nested_in_gua_drawer(v3_html: str):
    p = _Tree()
    p.feed(v3_html)
    assert "rulePickModal" in p.parent_of
    chain = []
    cur = p.parent_of.get("rulePickModal")
    seen: set[str] = set()
    while cur and cur not in seen:
        chain.append(cur)
        seen.add(cur)
        cur = p.parent_of.get(cur)
    assert "guaDrawer" not in chain, f"nested under guaDrawer: {chain}"

    gi = v3_html.find('id="guaDrawer"')
    mi = v3_html.find('id="rulePickModal"')
    assert 0 <= gi < mi
    between = v3_html[gi:mi]
    assert between.count("<div") == between.count("</div>"), (
        f"unbalanced open={between.count('<div')} close={between.count('</div>')}"
    )


def test_v3_rule_pick_css_and_button_wiring(v3_html: str):
    assert 'id="btnPickRules"' in v3_html
    assert 'id="rulePickModal"' in v3_html
    assert 'id="rulePickList"' in v3_html
    assert 'id="btnPickConfirm"' in v3_html
    assert re.search(r"rule-pick-modal\.show\{[^}]*display:\s*flex", v3_html)
    assert "z-index:5000" in v3_html or "z-index: 5000" in v3_html
    assert "async function openRulePicker" in v3_html
    assert "await loadRules()" in v3_html
    assert 'classList.add("show")' in v3_html
    assert "AppState.expRuleIds = Array.from(_pickTemp" in v3_html
    assert "openRulePicker" in v3_html


def test_v3_rule_pick_open_select_confirm_shipped_functions(v3_html: str):
    node = shutil.which("node")
    assert node, "node required"
    assert HARNESS.is_file(), HARNESS
    # ensure source under test still matches harness expectations
    assert "async function openRulePicker" in v3_html
    proc = subprocess.run(
        [node, str(HARNESS), str(V3)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert "PASS open/select/confirm" in proc.stdout


def test_v3_main_script_node_syntax(v3_html: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", v3_html, flags=re.I | re.S)
    assert scripts
    main = max(scripts, key=len)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as f:
        f.write(main)
        path = f.name
    try:
        proc = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
    finally:
        Path(path).unlink(missing_ok=True)
