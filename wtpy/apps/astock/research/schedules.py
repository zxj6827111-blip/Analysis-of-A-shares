# -*- coding: utf-8 -*-
"""Named research schedules and lightweight runner (Phase 6).

Pure-function configs — not a real Celery beat integration.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

# Named schedules as dict configs
SCHEDULES: Dict[str, Dict[str, Any]] = {
    "daily": {
        "name": "daily",
        "queues": ["research"],
        "max_trials": 50,
        "engine_default": "fast",
        "artifact_level": "minimal",
        "description": "Daytime continuous search with modest budget",
        "hour_utc": 1,
        "minute": 0,
        "weekdays": [0, 1, 2, 3, 4],  # Mon-Fri
    },
    "nightly": {
        "name": "nightly",
        "queues": ["research", "research_heavy"],
        "max_trials": 200,
        "engine_default": "full",
        "artifact_level": "standard",
        "description": "Overnight larger budget and full engine retests",
        "hour_utc": 16,
        "minute": 30,
        "weekdays": [0, 1, 2, 3, 4],
    },
    "weekend": {
        "name": "weekend",
        "queues": ["research", "research_heavy"],
        "max_trials": 500,
        "engine_default": "full",
        "artifact_level": "full",
        "description": "Weekend deep search and promotion retests",
        "hour_utc": 2,
        "minute": 0,
        "weekdays": [5, 6],  # Sat-Sun
    },
}


def list_schedules() -> List[Dict[str, Any]]:
    """Return all named schedule configs."""
    return [dict(SCHEDULES[k]) for k in sorted(SCHEDULES.keys())]


def get_schedule(name: str) -> Dict[str, Any]:
    """Get one schedule by name; raises KeyError if missing."""
    key = str(name or "").strip().lower()
    if key not in SCHEDULES:
        raise KeyError(f"unknown schedule: {name}")
    return dict(SCHEDULES[key])


def next_fire_times(name: str, now: Optional[datetime] = None, n: int = 3) -> List[datetime]:
    """Simple next-run heuristic (pure function, not Celery beat).

    Fires on configured weekdays at hour_utc:minute.
    """
    sched = get_schedule(name)
    if now is None:
        now = datetime.utcnow()
    weekdays = set(sched.get("weekdays") or list(range(7)))
    hour = int(sched.get("hour_utc") or 0)
    minute = int(sched.get("minute") or 0)
    n = max(1, int(n))
    out: List[datetime] = []
    # start from today
    cursor = now.replace(hour=0, minute=0, second=0, microsecond=0)
    guard = 0
    while len(out) < n and guard < 400:
        guard += 1
        if cursor.weekday() in weekdays:
            fire = cursor.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if fire > now:
                out.append(fire)
        cursor = cursor + timedelta(days=1)
    return out


class ScheduleRunner:
    """Enqueue N dummy research tasks via ResearchPlatform (for tests / dry-run)."""

    def __init__(self, platform: Any, schedule_name: str = "daily"):
        self.platform = platform
        self.schedule_name = schedule_name
        self.schedule = get_schedule(schedule_name)

    def enqueue_dummy(
        self,
        n: Optional[int] = None,
        *,
        experiment_id: Optional[str] = None,
        base_params: Optional[Dict[str, Any]] = None,
        handler: Optional[Callable[[dict], Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Enqueue up to max_trials (or n) dummy tasks on schedule queues."""
        max_trials = int(self.schedule.get("max_trials") or 1)
        count = max_trials if n is None else min(int(n), max_trials)
        queues: Sequence[str] = self.schedule.get("queues") or ["research"]
        q0 = queues[0] if queues else "research"
        exp = experiment_id or f"sched-{self.schedule_name}"
        base = dict(base_params or {"dummy": True, "schedule": self.schedule_name})
        results: List[Dict[str, Any]] = []
        for i in range(count):
            params = dict(base)
            params["idx"] = i
            params["engine"] = self.schedule.get("engine_default")
            params["artifact_level"] = self.schedule.get("artifact_level")
            out = self.platform.enqueue_trial(
                experiment_id=exp,
                params=params,
                queue=q0,
                idempotency_key=f"{exp}:{self.schedule_name}:{i}",
                extra_payload={"schedule": self.schedule_name},
            )
            results.append(out)
        if handler is not None and hasattr(self.platform, "make_worker"):
            # optional: attach a one-shot worker id for tests
            wid = f"sched-runner-{self.schedule_name}"
            try:
                self.platform.make_worker(wid, handler, queues=list(queues))
            except Exception:
                pass
        return results


__all__ = [
    "SCHEDULES",
    "list_schedules",
    "get_schedule",
    "next_fire_times",
    "ScheduleRunner",
]
