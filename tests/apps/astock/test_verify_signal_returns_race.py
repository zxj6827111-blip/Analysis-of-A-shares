# -*- coding: utf-8 -*-
"""独立验证：信号周涨幅前端竞态 / 实验中心并发下拉 / 文档与脚本一致性。

不代表 coder 用例；node 桩为独立编写（v3_signal_returns_race_harness.js）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

ROOT = Path(__file__).resolve().parents[3]
V3 = ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index_v3.html"
HARNESS = Path(__file__).resolve().parent / "v3_signal_returns_race_harness.js"
DOC = ROOT / "docs" / "api_partners.md"
BAGUA_ROUTE = ROOT / "wtpy" / "apps" / "astock" / "api_routes" / "bagua.py"
BAGUA_SERVICE = ROOT / "wtpy" / "apps" / "astock" / "service" / "bagua_query.py"
TOOL = ROOT / "tools" / "verify_export_history_date.py"

RACE_MARKERS = (
    "PASS race switch-B-issues-A",
    "PASS race switch-B-issues-B",
    "PASS race switch-B-renders-B",
    "PASS race switch-B-late-A-dropped",
    "PASS race horizon-two-requests",
    "PASS race horizon-10-renders",
    "PASS race horizon-5-late-dropped",
    "PASS race stale-exception-cannot-overwrite",
    "PASS race old-finally-does-not-clear-loading",
    "PASS race latest-finally-clears-loading",
    "PASS race visual-zero-fetch",
    "PASS race visual-demo-rows",
    "PASS race visual-meta-demo",
    "PASS race visual-no-pollution",
    "PASS race dropdown-adds-dynamic",
    "PASS race dropdown-cleans-dynamic-on-static",
    "PASS race dropdown-readd-no-duplicate",
    "PASS race dropdown-switch-dynamic",
    "PASS race dropdown-respects-user-touched",
)


def test_signal_returns_race_harness():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    assert V3.is_file() and HARNESS.is_file()
    proc = subprocess.run(
        [node, str(HARNESS), str(V3)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    for marker in RACE_MARKERS:
        assert marker in proc.stdout, marker


def test_api_partners_review_contract_matches_implementation():
    doc = DOC.read_text(encoding="utf-8")
    route = BAGUA_ROUTE.read_text(encoding="utf-8")
    service = BAGUA_SERVICE.read_text(encoding="utf-8")

    # review_rules：POST 数组 / GET 逗号串 / 空值语义
    assert "review_rules" in doc
    assert 'review_rules: Optional[List[str]] = None' in route
    assert "review_rules: Optional[str] = Query(" in route
    assert "不附带任何信号 sheet" in doc
    assert "select:未勾选任何信号规则" in service

    # 信号 sheet 生成规则：precomputed / computed / placeholder
    for token in ("precomputed:", "computed:", "placeholder:"):
        assert token in doc, token
        assert token in service, token
    assert 'review_rule_sources[rid or sheet] = f"{source}:{src_asof}"' in service
    assert 'review_rule_sources[rid] = f"placeholder:{base_asof}"' in service

    # meta 字段：doc 与实现共用同一组 indicator_review_* 字段名
    for field in (
        "indicator_review_asof",
        "indicator_review_query_date",
        "indicator_review_sheets",
        "indicator_review_rules_selected",
        "indicator_review_rule_sources",
        "indicator_review_placeholders",
        "indicator_review_note",
    ):
        assert field in doc, field
        assert '"%s"' % field in service, field

    # 同步导出响应头 + 异步 job JSON 字段
    for header in (
        "X-Bagua-Review-AsOf",
        "X-Bagua-Review-Note",
        "X-Bagua-Review-Fallback",
    ):
        assert header in doc, header
        assert header in route, header
    assert "urllib.parse.unquote" in doc
    assert '_quote(note, safe="")' in route
    for key in ("review_asof_used", "review_note", "review_fallback"):
        assert key in doc, key
        assert key in route, key


def test_verify_export_history_date_tool_is_portable():
    src = TOOL.read_text(encoding="utf-8")
    assert "--rizhu-path" in src
    assert "argparse" in src
    # 不得残留个人机器路径 / 特定个人文件名 / 盘符硬编码
    for bad in ("zxj68", "股票+卦象", "D:\\", "C:\\", "/Users/"):
        assert bad not in src, bad

    proc = subprocess.run(
        [sys.executable, "-m", "tools.verify_export_history_date", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "--rizhu-path" in proc.stdout
