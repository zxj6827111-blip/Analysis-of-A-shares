# -*- coding: utf-8 -*-
"""Bagua yao allowlists + flexible gua_filter for backtests.

Default product policy (2026-07): when the user includes 八卦OHLC / with_bagua
without an explicit gua_filter, signals are filtered by **最佳3爻**
(user judgment list from the 64-gua study), not merely labelled.

New product path (2026-07-21): ``gua_filter`` config supports main_hexagram /
exact_line / action_signal / combined selection modes with stable state_id keys.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# 用户确认的「最佳3爻」：地雷复初九 / 地风升初六 / 地天泰初九
BEST3: List[Tuple[str, str]] = [
    ("地雷复", "初九"),
    ("地风升", "初六"),
    ("地天泰", "初九"),
]

BAGUA_MODE_BEST3 = "best3"
DEFAULT_BAGUA_FILTER_MODE = BAGUA_MODE_BEST3

BAGUA_MODE_LABELS = {
    BAGUA_MODE_BEST3: "八卦最佳3爻",
}

# selection_mode values
MODE_NONE = "none"
MODE_MAIN = "main_hexagram"
MODE_EXACT = "exact_line"
MODE_ACTION = "action_signal"
MODE_COMBINED = "combined"

KNOWN_ACTION_SIGNALS = ("新开仓", "加仓", "持有", "减仓", "清仓")

DEFAULT_RULE_VERSION = "gua_rules_v20260721"


def strip_gua(name: str) -> str:
    if not name:
        return ""
    s = str(name)
    while s and not ("\u4e00" <= s[0] <= "\u9fff"):
        s = s[1:]
    return s


def pair_from_bagua(bg: Optional[dict]) -> Optional[Tuple[str, str]]:
    if not bg:
        return None
    full = bg.get("full_name") or bg.get("gua_name") or bg.get("main_hexagram_name") or ""
    yao = bg.get("yao_name") or bg.get("line_name") or ""
    gua = strip_gua(full)
    if not gua or not yao:
        return None
    return gua, str(yao)


def match_allow(gua: str, yao: str, allow: Sequence[Tuple[str, str]]) -> bool:
    for key, y in allow:
        if yao != y:
            continue
        if gua == key or key in gua or gua in key:
            return True
    return False


def allowlist_for_mode(mode: str) -> Sequence[Tuple[str, str]]:
    m = (mode or DEFAULT_BAGUA_FILTER_MODE).strip().lower()
    if m in ("best3", "user_best3", "最佳3爻", "最佳三爻"):
        return BEST3
    raise ValueError(f"unsupported bagua filter mode: {mode!r} (only best3 is enabled)")


def mode_label(mode: str) -> str:
    m = (mode or DEFAULT_BAGUA_FILTER_MODE).strip().lower()
    if m in ("best3", "user_best3", "最佳3爻", "最佳三爻"):
        return BAGUA_MODE_LABELS[BAGUA_MODE_BEST3]
    return f"八卦:{mode}"


def event_matches_allow(ev, allow: Sequence[Tuple[str, str]]) -> bool:
    bg = getattr(ev, "bagua", None) or {}
    if isinstance(bg, dict):
        p = pair_from_bagua(bg)
    else:
        p = None
    if not p:
        return False
    gua, yao = p
    return match_allow(gua, yao, allow)


def filter_events_by_bagua_mode(events: Iterable, mode: str = DEFAULT_BAGUA_FILTER_MODE) -> List:
    """Keep only events whose attached bagua (卦, 爻) is on the allowlist for mode."""
    allow = allowlist_for_mode(mode)
    out = []
    for ev in events:
        if event_matches_allow(ev, allow):
            out.append(ev)
    return out


def best3_display_pairs() -> List[str]:
    return [f"{g}|{y}" for g, y in BEST3]


# ---------------------------------------------------------------------------
# New gua_filter model
# ---------------------------------------------------------------------------


@dataclass
class GuaFilter:
    """Structured hexagram filter configuration (persisted on each run)."""

    enabled: bool = False
    selection_mode: str = MODE_NONE  # none|main_hexagram|exact_line|action_signal|combined
    match_mode: str = "any"  # reserved; within-category OR, across AND
    selected_main_hexagram_ids: List[int] = field(default_factory=list)
    selected_state_ids: List[str] = field(default_factory=list)
    selected_action_signals: List[str] = field(default_factory=list)
    rule_version: str = DEFAULT_RULE_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "GuaFilter":
        if not raw or not isinstance(raw, dict):
            return cls()
        mode = str(raw.get("selection_mode") or MODE_NONE).strip() or MODE_NONE
        enabled = bool(raw.get("enabled", False))
        if mode == MODE_NONE:
            enabled = False
        mains = raw.get("selected_main_hexagram_ids") or []
        states = raw.get("selected_state_ids") or []
        actions = raw.get("selected_action_signals") or []
        return cls(
            enabled=enabled,
            selection_mode=mode,
            match_mode=str(raw.get("match_mode") or "any"),
            selected_main_hexagram_ids=[int(x) for x in mains if x is not None and str(x) != ""],
            selected_state_ids=[str(x).strip() for x in states if str(x).strip()],
            selected_action_signals=[str(x).strip() for x in actions if str(x).strip()],
            rule_version=str(raw.get("rule_version") or DEFAULT_RULE_VERSION),
        )

    def is_active(self) -> bool:
        if not self.enabled:
            return False
        if self.selection_mode in (MODE_NONE, "", None):
            return False
        if self.selection_mode == MODE_MAIN:
            return bool(self.selected_main_hexagram_ids)
        if self.selection_mode == MODE_EXACT:
            return bool(self.selected_state_ids)
        if self.selection_mode == MODE_ACTION:
            return bool(self.selected_action_signals)
        if self.selection_mode == MODE_COMBINED:
            return bool(
                self.selected_main_hexagram_ids
                or self.selected_state_ids
                or self.selected_action_signals
            )
        return False


def normalize_state_id(bg: dict) -> Optional[str]:
    if not bg:
        return None
    sid = bg.get("state_id")
    if sid:
        return str(sid)
    go = bg.get("gua_order") or bg.get("main_hexagram_id")
    yo = bg.get("yao_order") or bg.get("line_index")
    if go is None or yo is None:
        return None
    try:
        return f"{int(go):02d}-{int(yo)}"
    except (TypeError, ValueError):
        return None


def enrich_bagua_dict(bg: dict, knowledge_entry: Optional[dict] = None) -> dict:
    """Ensure bagua dict carries state_id / action_signal / standardized fields."""
    if not bg:
        return bg
    out = dict(bg)
    if knowledge_entry:
        for k in (
            "state_id",
            "action_signal",
            "biangua",
            "changed_hexagram_name",
            "changed_hexagram_id",
            "market_summary",
            "market_judgement",
            "line_text",
            "yao_ci",
            "main_hexagram_name",
            "main_hexagram_id",
            "hexagram_symbol",
            "line_name",
            "line_index",
        ):
            if knowledge_entry.get(k) not in (None, "") and out.get(k) in (None, ""):
                out[k] = knowledge_entry[k]
        # prefer knowledge action_signal
        if knowledge_entry.get("action_signal"):
            out["action_signal"] = knowledge_entry["action_signal"]
        if knowledge_entry.get("state_id"):
            out["state_id"] = knowledge_entry["state_id"]
        if knowledge_entry.get("biangua") is not None:
            out["biangua"] = knowledge_entry.get("biangua") or ""
            out["changed_hexagram_name"] = knowledge_entry.get("changed_hexagram_name") or out["biangua"]
    if not out.get("state_id"):
        sid = normalize_state_id(out)
        if sid:
            out["state_id"] = sid
    if out.get("action_signal") is None:
        out["action_signal"] = ""
    if out.get("biangua") is None:
        out["biangua"] = ""
    return out


def event_matches_gua_filter(ev, gf: GuaFilter) -> bool:
    """AND across non-empty categories; OR within each category."""
    if not gf.is_active():
        return True
    bg = getattr(ev, "bagua", None) or {}
    if not isinstance(bg, dict) or not bg:
        return False

    mode = gf.selection_mode
    checks: List[bool] = []

    # main hexagram dimension
    main_ids = set(int(x) for x in gf.selected_main_hexagram_ids)
    state_ids = set(str(x) for x in gf.selected_state_ids)
    actions = set(str(x) for x in gf.selected_action_signals)

    go = bg.get("gua_order") or bg.get("main_hexagram_id")
    try:
        go_i = int(go) if go is not None else None
    except (TypeError, ValueError):
        go_i = None
    sid = normalize_state_id(bg)
    act = str(bg.get("action_signal") or "").strip()

    if mode == MODE_MAIN:
        if not main_ids:
            return False
        return go_i is not None and go_i in main_ids

    if mode == MODE_EXACT:
        if not state_ids:
            return False
        return sid is not None and sid in state_ids

    if mode == MODE_ACTION:
        if not actions:
            return False
        return act in actions

    # combined: AND of non-empty groups
    if main_ids:
        checks.append(go_i is not None and go_i in main_ids)
    if state_ids:
        checks.append(sid is not None and sid in state_ids)
    if actions:
        checks.append(act in actions)
    if not checks:
        return False
    return all(checks)


def filter_events_by_gua_filter(events: Iterable, gf: GuaFilter) -> List:
    if not gf.is_active():
        return list(events)
    return [ev for ev in events if event_matches_gua_filter(ev, gf)]


def compute_bagua_metrics(
    events: Iterable,
    gf: Optional[GuaFilter] = None,
    *,
    min_sample: int = 30,
) -> Dict[str, Any]:
    """Aggregate bagua distribution on signal events (before/after filter).

    Returns counts by main hexagram, action_signal, state_id, plus sample-size flags.
    Designed for run detail + research preview; does not re-run the trading engine.
    """
    all_events = list(events or [])
    n_total = len(all_events)
    gf_active = bool(gf and gf.is_active())
    kept = (
        [ev for ev in all_events if event_matches_gua_filter(ev, gf)]  # type: ignore[arg-type]
        if gf_active
        else list(all_events)
    )
    n_kept = len(kept)

    def _agg(evs: List) -> Dict[str, Any]:
        by_main: Dict[str, int] = {}
        by_action: Dict[str, int] = {}
        by_state: Dict[str, int] = {}
        missing_bagua = 0
        for ev in evs:
            bg = getattr(ev, "bagua", None) or {}
            if not isinstance(bg, dict) or not bg:
                missing_bagua += 1
                continue
            go = bg.get("gua_order") or bg.get("main_hexagram_id")
            name = (
                bg.get("main_hexagram_name")
                or bg.get("gua_name")
                or bg.get("full_name")
                or ""
            )
            if go is not None:
                try:
                    key = f"{int(go):02d}"
                except (TypeError, ValueError):
                    key = str(go)
                label = f"{key}:{name}" if name else key
                by_main[label] = by_main.get(label, 0) + 1
            act = str(bg.get("action_signal") or "").strip() or "(空)"
            by_action[act] = by_action.get(act, 0) + 1
            sid = normalize_state_id(bg) or str(bg.get("state_id") or "")
            if sid:
                by_state[sid] = by_state.get(sid, 0) + 1
        # top lists
        def top(d: Dict[str, int], n: int = 15) -> List[Dict[str, Any]]:
            items = sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
            return [{"key": k, "count": c} for k, c in items]

        return {
            "n": len(evs),
            "missing_bagua": missing_bagua,
            "by_main_hexagram": top(by_main),
            "by_action_signal": top(by_action, 10),
            "by_state_id": top(by_state, 20),
            "n_unique_main": len(by_main),
            "n_unique_state": len(by_state),
        }

    before = _agg(all_events)
    after = _agg(kept)
    sample_ok = n_kept >= int(min_sample)
    retention = (n_kept / n_total) if n_total else 0.0
    warnings: List[str] = []
    if n_total == 0:
        warnings.append("无技术信号样本，无法评估卦象过滤效果。")
    elif not sample_ok:
        warnings.append(
            f"过滤后仅 {n_kept} 条信号（建议 ≥{min_sample}），统计可能不稳健。"
        )
    if after.get("missing_bagua"):
        warnings.append(
            f"过滤后仍有 {after['missing_bagua']} 条信号缺少卦象标注。"
        )
    return {
        "n_signals_before": n_total,
        "n_signals_after": n_kept,
        "retention_rate": retention,
        "filter_active": gf_active,
        "selection_mode": (gf.selection_mode if gf else MODE_NONE),
        "min_sample": int(min_sample),
        "sample_sufficient": sample_ok,
        "before": before,
        "after": after,
        "warnings": warnings,
    }


def gua_filter_natural_language(
    gf: GuaFilter,
    *,
    hexagram_names: Optional[Dict[int, str]] = None,
) -> str:
    """Human-readable match rule (no raw AND/OR symbols for end users)."""
    if not gf or not gf.is_active():
        return "卦象过滤：未启用"
    hexagram_names = hexagram_names or {}
    parts: List[str] = []
    mode = gf.selection_mode

    def names_for_ids(ids: Sequence[int]) -> str:
        labels = []
        for i in ids:
            nm = hexagram_names.get(int(i)) or f"第{int(i):02d}卦"
            labels.append(nm)
        if len(labels) == 1:
            return labels[0]
        if len(labels) <= 4:
            return "或".join(labels)
        return "、".join(labels[:3]) + f"等{len(labels)}个主卦"

    if mode == MODE_MAIN and gf.selected_main_hexagram_ids:
        parts.append(f"主卦属于{names_for_ids(gf.selected_main_hexagram_ids)}")
    elif mode == MODE_EXACT and gf.selected_state_ids:
        n = len(gf.selected_state_ids)
        parts.append(f"精确爻象命中已选的{n}条状态")
    elif mode == MODE_ACTION and gf.selected_action_signals:
        acts = "或".join(gf.selected_action_signals)
        parts.append(f"操作信号属于{acts}")
    elif mode == MODE_COMBINED:
        if gf.selected_main_hexagram_ids:
            parts.append(f"主卦属于{names_for_ids(gf.selected_main_hexagram_ids)}")
        if gf.selected_state_ids:
            parts.append(f"精确爻象命中已选的{len(gf.selected_state_ids)}条状态")
        if gf.selected_action_signals:
            parts.append(f"操作信号属于{'或'.join(gf.selected_action_signals)}")

    if not parts:
        return "卦象过滤：未启用"
    if len(parts) == 1:
        return parts[0] + "。"
    return "，并且".join(parts) + "。"


def gua_filter_history_summary(
    gf: Optional[GuaFilter],
    *,
    hexagram_names: Optional[Dict[int, str]] = None,
    state_labels: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Compact labels for history list + tooltip lines."""
    if not gf or not gf.is_active():
        return {
            "enabled": False,
            "short": "卦象过滤：未启用",
            "tooltip_lines": [],
            "count": 0,
        }
    hexagram_names = hexagram_names or {}
    state_labels = state_labels or {}
    mode = gf.selection_mode
    lines: List[str] = []
    short = ""
    count = 0
    if mode == MODE_ACTION:
        acts = list(gf.selected_action_signals)
        short = "卦象信号：" + "、".join(acts[:3]) + ("…" if len(acts) > 3 else "")
        lines = acts
        count = len(acts)
    elif mode == MODE_MAIN:
        ids = list(gf.selected_main_hexagram_ids)
        count = len(ids)
        labels = [hexagram_names.get(int(i), f"第{int(i):02d}卦") for i in ids]
        short = f"卦象{count}项" if count else "卦象过滤"
        lines = labels
    elif mode in (MODE_EXACT, MODE_COMBINED):
        sids = list(gf.selected_state_ids)
        if sids:
            count = len(sids)
            lines = [state_labels.get(s, s) for s in sids]
            # Prefer concrete names over bare "卦象N项" (compare UI / task titles).
            if count == 1:
                short = str(lines[0]) if lines else "卦象1项"
            elif count <= 3:
                joined = "、".join(str(x) for x in lines)
                short = joined if len(joined) <= 36 else ("卦象" + str(count) + "项：" + "、".join(str(x) for x in lines[:2]) + "…")
            else:
                short = "卦象" + str(count) + "项：" + "、".join(str(x) for x in lines[:2]) + "…"
        elif gf.selected_main_hexagram_ids:
            ids = list(gf.selected_main_hexagram_ids)
            count = len(ids)
            lines = [hexagram_names.get(int(i), f"第{int(i):02d}卦") for i in ids]
            if count == 1:
                short = str(lines[0]) if lines else "卦象1项"
            elif count <= 3:
                joined = "、".join(str(x) for x in lines)
                short = joined if len(joined) <= 36 else ("卦象" + str(count) + "项")
            else:
                short = "卦象" + str(count) + "项：" + "、".join(str(x) for x in lines[:2]) + "…"
        elif gf.selected_action_signals:
            acts = list(gf.selected_action_signals)
            count = len(acts)
            short = "操作信号：" + "、".join(acts[:3]) + ("…" if len(acts) > 3 else "")
            lines = acts
        else:
            short = "卦象过滤"
    else:
        short = "卦象过滤"
    return {
        "enabled": True,
        "short": short,
        "tooltip_lines": lines,
        "count": count,
        "selection_mode": mode,
        "natural_language": gua_filter_natural_language(gf, hexagram_names=hexagram_names),
    }
