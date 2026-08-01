# -*- coding: utf-8 -*-
"""Task-center KPI status semantics."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
V3 = ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index_v3.html"
HARNESS = Path(__file__).resolve().parent / "task_center_kpi_harness.js"


def test_task_center_kpi_labels_and_status_summary():
    html = V3.read_text(encoding="utf-8")
    assert '<div class="label">已结束</div>' in html
    assert 'id="tkDoneTrend"' in html
    assert '<div class="label">需关注</div>' in html
    assert 'id="tkFailedTrend"' in html
    assert "function taskStatusGroup" in html
    assert "function summarizeTaskStatuses" in html


def test_task_center_kpi_counts_terminal_and_attention_statuses():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, str(HARNESS), str(V3)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert "PASS task-center KPI summary" in proc.stdout
