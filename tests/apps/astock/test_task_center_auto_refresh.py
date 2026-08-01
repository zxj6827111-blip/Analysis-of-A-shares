# -*- coding: utf-8 -*-
"""Task-center refresh wiring for terminal backtest jobs."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
V3 = ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index_v3.html"


def _slice(html: str, start: str, end: str) -> str:
    start_pos = html.index(start)
    end_pos = html.index(end, start_pos)
    return html[start_pos:end_pos]


def test_task_center_refreshes_after_queue_state_changes():
    html = V3.read_text(encoding="utf-8")
    queue_refresh = _slice(
        html,
        "async function refreshTaskQueueBar()",
        "async function cancelBacktestJob",
    )

    assert "refreshTaskCenterOnQueueChange(snap);" in queue_refresh
    assert "function taskQueueStateSignature(snap)" in html
    assert "if (!changed || !taskCenterListIsActive()) return;" in html


def test_terminal_job_poll_refreshes_task_center_list():
    html = V3.read_text(encoding="utf-8")
    poller = _slice(
        html,
        "function trackBtJobInBackground(jobId)",
        "function buildBacktestBody()",
    )
    succeeded = _slice(
        poller,
        'if (j.status === "succeeded")',
        'if (j.status === "failed" || j.status === "cancelled")',
    )
    failed = _slice(
        poller,
        'if (j.status === "failed" || j.status === "cancelled")',
        "setTimeout(tick, 1200);",
    )

    assert "await refreshTaskCenterList();" in succeeded
    assert "await refreshTaskCenterList();" in failed
