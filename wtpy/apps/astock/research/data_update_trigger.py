# -*- coding: utf-8 -*-
"""Triggers after market data updates: retest enqueue + drift alert payload."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .drift import detect_drift


def on_market_data_updated(
    platform: Any,
    candidates: List[Dict[str, Any]],
    *,
    experiment_id: str = "data-update-retest",
    queue: str = "research",
    max_candidates: int = 50,
) -> Dict[str, Any]:
    """Re-enqueue retest trials for candidate param sets after data refresh.

    Each candidate may be a params dict or ``{params: {...}, id?: str}``.
    """
    candidates = list(candidates or [])[: max(0, int(max_candidates))]
    enqueued: List[Dict[str, Any]] = []
    for i, cand in enumerate(candidates):
        if not isinstance(cand, dict):
            continue
        params = cand.get("params") if isinstance(cand.get("params"), dict) else dict(cand)
        # strip non-param keys commonly used on trial rows
        for k in ("id", "trial_id", "metrics", "score", "status", "created"):
            params.pop(k, None)
        params = dict(params)
        params["retest"] = True
        params["trigger"] = "market_data_updated"
        cid = cand.get("id") or cand.get("trial_id") or params.get("param_hash") or i
        out = platform.enqueue_trial(
            experiment_id=experiment_id,
            params=params,
            queue=queue,
            idempotency_key=f"{experiment_id}:retest:{cid}",
            extra_payload={"trigger": "market_data_updated", "candidate_id": str(cid)},
        )
        enqueued.append(out)
    return {
        "ok": True,
        "trigger": "market_data_updated",
        "n_candidates": len(candidates),
        "enqueued": len(enqueued),
        "results": enqueued,
    }


def monitor_drift_and_alert(
    recent: Dict[str, Any],
    baseline: Dict[str, Any],
    *,
    thresholds: Optional[Dict[str, float]] = None,
    source: str = "research",
) -> Dict[str, Any]:
    """Wrap detect_drift and build an alert-shaped payload."""
    drift = detect_drift(recent, baseline, thresholds=thresholds)
    severity = str(drift.get("severity") or "none")
    alert: Dict[str, Any] = {
        "alert": bool(drift.get("drift")),
        "level": severity if drift.get("drift") else "none",
        "title": "Performance drift detected" if drift.get("drift") else "No drift",
        "message": "; ".join(drift.get("reasons") or []) or "metrics within thresholds",
        "source": source,
        "reasons": list(drift.get("reasons") or []),
        "severity": severity,
        "deltas": drift.get("deltas") or {},
        "thresholds": drift.get("thresholds") or {},
        "recent": recent,
        "baseline": baseline,
    }
    return {
        "ok": True,
        "drift": bool(drift.get("drift")),
        "severity": severity,
        "reasons": list(drift.get("reasons") or []),
        "deltas": drift.get("deltas") or {},
        "thresholds": drift.get("thresholds") or {},
        "alert": alert,
    }


__all__ = ["on_market_data_updated", "monitor_drift_and_alert"]
