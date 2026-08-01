# -*- coding: utf-8 -*-
"""Reconcile/backfill SQLite run registry from on-disk run artifacts (D5).

Backfills runs that exist as runs_index.json rows and/or run_meta.json
directories but are missing from the SQLite runs table (e.g. runs recorded
while the un-migrated v2 production DB silently rejected upserts).

Safe by design:
- never deletes or rewrites existing SQLite rows (idempotent inserts only);
- only writes fields actually present in the on-disk artifacts;
- --dry-run previews without writing;
- prints and (with --out) saves a JSON reconcile report.

Usage:
    python scripts/reconcile_sqlite_runs.py [--dry-run] [--out report.json]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wtpy.apps.astock.config import get_default_config, load_env_file  # noqa: E402
from wtpy.apps.astock.service.db import reconcile_runs_from_disk  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="reconcile_sqlite_runs")
    p.add_argument("--dry-run", action="store_true", help="preview only, no writes")
    p.add_argument("--out", default=None, help="write JSON report to this path")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    load_env_file()
    cfg = get_default_config()
    report = reconcile_runs_from_disk(cfg, dry_run=bool(args.dry_run))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
