# -*- coding: utf-8 -*-
"""Artifact level helpers for research trials (summary vs full)."""

from __future__ import annotations

from typing import Dict, Optional, Set

ARTIFACT_LEVELS = ("summary", "candidate", "full")


def normalize_artifact_level(value: Optional[str], *, default: str = "full") -> str:
    if value is None or str(value).strip() == "":
        return default
    v = str(value).strip().lower()
    aliases = {
        "min": "summary",
        "minimal": "summary",
        "meta": "summary",
        "light": "candidate",
        "detail": "full",
        "complete": "full",
    }
    v = aliases.get(v, v)
    if v not in ARTIFACT_LEVELS:
        return default
    return v


def should_write(name: str, level: str) -> bool:
    level = normalize_artifact_level(level)
    summary_ok: Set[str] = {"run_meta.json", "metrics.json", "summary.json"}
    candidate_ok = summary_ok | {"equity_curve.csv", "year_metrics.json"}
    if level == "summary":
        return name in summary_ok
    if level == "candidate":
        return name in candidate_ok
    return True


def apply_artifact_policy(
    *,
    level: str,
    write_signals: bool = True,
    write_fills: bool = True,
    write_excel: bool = True,
    write_equity: bool = True,
) -> Dict[str, bool]:
    level = normalize_artifact_level(level)
    if level == "summary":
        return {
            "write_signals": False,
            "write_fills": False,
            "write_excel": False,
            "write_equity": False,
            "write_meta": True,
        }
    if level == "candidate":
        return {
            "write_signals": False,
            "write_fills": False,
            "write_excel": False,
            "write_equity": write_equity,
            "write_meta": True,
        }
    return {
        "write_signals": write_signals,
        "write_fills": write_fills,
        "write_excel": write_excel,
        "write_equity": write_equity,
        "write_meta": True,
    }
