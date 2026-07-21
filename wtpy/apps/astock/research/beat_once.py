# -*- coding: utf-8 -*-
"""CLI: one-shot schedule beat tick without Celery.

Usage::

    python -m wtpy.apps.astock.research.beat_once --store PATH --storage PATH [--dry-run]
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run one research schedule beat tick")
    p.add_argument(
        "--store",
        type=str,
        default=None,
        help="Path to last_fire JSON store (default: <storage>/schedule_beat.json)",
    )
    p.add_argument(
        "--storage",
        type=str,
        default="storage/astock",
        help="ResearchPlatform storage root",
    )
    p.add_argument("--dry-run", action="store_true", help="Do not enqueue or update store")
    p.add_argument(
        "--now",
        type=str,
        default=None,
        help="Override now as ISO datetime (UTC naive)",
    )
    args = p.parse_args(argv)

    from .platform import ResearchPlatform
    from .schedule_runner import beat_once

    storage = Path(args.storage)
    store = Path(args.store) if args.store else storage / "schedule_beat.json"
    now = None
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", ""))
    plat = ResearchPlatform(storage, use_memory_queue=True)
    try:
        result = beat_once(store, plat, now=now, dry_run=bool(args.dry_run))
    finally:
        plat.close()
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
