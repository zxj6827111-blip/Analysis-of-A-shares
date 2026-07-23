# -*- coding: utf-8 -*-
"""File-based schedule beat simulator (no Celery required).

Durable last-fire store + due detection + budgeted fire via ResearchPlatform.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .schedules import SCHEDULES, ScheduleRunner, get_schedule, list_schedules, next_fire_times

# Hard safety cap on enqueued trials per fire (even if schedule max_trials is higher)
FIRE_MAX_TRIALS_CAP = 20


class ScheduleBeatStore:
    """Persist last_fire timestamps for named schedules (JSON file)."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            self._data = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                fires = raw.get("last_fire") if isinstance(raw.get("last_fire"), dict) else raw
                self._data = {
                    str(k): str(v) for k, v in (fires or {}).items() if v is not None
                }
            else:
                self._data = {}
        except (OSError, json.JSONDecodeError, TypeError):
            self._data = {}

    def _save(self) -> None:
        payload = {"last_fire": dict(self._data)}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def get_last_fire(self, name: str) -> Optional[datetime]:
        key = str(name or "").strip().lower()
        s = self._data.get(key)
        if not s:
            return None
        try:
            # support both ISO with/without Z
            return datetime.fromisoformat(s.replace("Z", "+00:00").replace("+00:00", ""))
        except ValueError:
            try:
                return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                return None

    def set_last_fire(self, name: str, when: Optional[datetime] = None) -> datetime:
        key = str(name or "").strip().lower()
        when = when or datetime.utcnow()
        self._data[key] = when.replace(microsecond=0).isoformat()
        self._save()
        return when

    def all_last_fires(self) -> Dict[str, Optional[str]]:
        out: Dict[str, Optional[str]] = {}
        for name in SCHEDULES:
            out[name] = self._data.get(name)
        for k, v in self._data.items():
            if k not in out:
                out[k] = v
        return out


def _scheduled_fire_for_day(sched: Dict[str, Any], day: datetime) -> datetime:
    hour = int(sched.get("hour_utc") or 0)
    minute = int(sched.get("minute") or 0)
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _last_scheduled_slot_on_or_before(name: str, now: datetime) -> Optional[datetime]:
    """Most recent configured fire time at or before ``now`` (look back ~14 days)."""
    sched = get_schedule(name)
    weekdays = set(sched.get("weekdays") or list(range(7)))
    cursor = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(21):
        if cursor.weekday() in weekdays:
            fire = _scheduled_fire_for_day(sched, cursor)
            if fire <= now:
                return fire
        cursor = cursor - timedelta(days=1)
    return None


def due_schedules(
    now: Optional[datetime] = None,
    store: Optional[ScheduleBeatStore] = None,
) -> List[str]:
    """Return schedule names that should fire at ``now`` given last_fire in store.

    A schedule is due if the last configured slot on or before ``now`` exists and
    has not yet been recorded as fired (last_fire < that slot, or missing).
    """
    if now is None:
        now = datetime.utcnow()
    due: List[str] = []
    for name in sorted(SCHEDULES.keys()):
        slot = _last_scheduled_slot_on_or_before(name, now)
        if slot is None:
            continue
        last = store.get_last_fire(name) if store is not None else None
        if last is None or last < slot:
            due.append(name)
    return due


def fire_schedule(
    name: str,
    platform: Any,
    *,
    dry_run: bool = False,
    store: Optional[ScheduleBeatStore] = None,
    now: Optional[datetime] = None,
    n: Optional[int] = None,
    experiment_id: Optional[str] = None,
    base_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Enqueue budgeted dummy/search trials for a named schedule.

    Caps enqueued count at ``min(max_trials, FIRE_MAX_TRIALS_CAP)`` (or ``n`` if
    provided and smaller). When ``dry_run=True``, does not enqueue or update store.
    """
    sched = get_schedule(name)
    max_trials = int(sched.get("max_trials") or 1)
    budget = min(max_trials, FIRE_MAX_TRIALS_CAP)
    if n is not None:
        budget = min(budget, max(0, int(n)))
    when = now or datetime.utcnow()

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "schedule": name,
            "budget": budget,
            "enqueued": 0,
            "would_enqueue": budget,
            "schedule_config": sched,
            "now": when.isoformat(),
        }

    runner = ScheduleRunner(platform, name)
    results = runner.enqueue_dummy(
        n=budget,
        experiment_id=experiment_id,
        base_params=base_params or {"dummy": True, "schedule": name, "fire": True},
    )
    if store is not None:
        store.set_last_fire(name, when)
    return {
        "ok": True,
        "dry_run": False,
        "schedule": name,
        "budget": budget,
        "enqueued": len(results),
        "results": results,
        "schedule_config": sched,
        "now": when.isoformat(),
    }


def beat_once(
    store_path: Union[str, Path],
    platform: Any,
    *,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Single beat tick: fire all due schedules."""
    store = ScheduleBeatStore(store_path)
    when = now or datetime.utcnow()
    due = due_schedules(when, store)
    fires: List[Dict[str, Any]] = []
    for name in due:
        fires.append(fire_schedule(name, platform, dry_run=dry_run, store=store, now=when))
    return {
        "ok": True,
        "now": when.isoformat(),
        "due": due,
        "fires": fires,
        "store": str(Path(store_path)),
    }


__all__ = [
    "ScheduleBeatStore",
    "due_schedules",
    "fire_schedule",
    "beat_once",
    "FIRE_MAX_TRIALS_CAP",
    "list_schedules",
    "get_schedule",
    "next_fire_times",
]
