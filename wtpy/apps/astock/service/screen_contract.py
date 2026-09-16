# -*- coding: utf-8 -*-
"""按规则筛选自动预计算 + 入选股票次周跟踪 — 数据契约（v4.1，阶段 0）。

本模块只承载**契约**：身份三元组、状态枚举、自然周对齐、数据版本捕获、
tracking revision、分口径覆盖率、发布策略判定、任务状态/待办记录。
不做任何全市场扫描或收益计算（阶段 1/2 的服务层职责）。

契约要点（docs/plans/auto-screen-track/contract.md 为权威全文）：

- snapshot_id  = 独立运行 ID（O_EXCL 独占创建，绝不覆盖既有文件）；
                 content_fingerprint 另存，仅用于"同内容重跑"的幂等提示。
- week_id      = 信号自然周的最后一个交易日（YYYYMMDD）。整周休市不产生
                 week_id，绝不用前一周最后交易日顶替（防重复发布名单）。
- published_snapshot_id = 周索引里的正式发布指针；统计/汇总/跟踪只认指针。
                 后台重试、backfill（已有任何指针时）、recompute 均不自动替换。
- track 产物不可变：track_{snapshot_id}_{tracking_revision_id}.json；
                 运行状态（pending/failed 等）存独立的任务状态记录。
- complete 只表示"本轮评估已结束"，不表示数据完整：覆盖率按口径分别
  记录，应评估样本为 0 时覆盖率返回 null（不是 0、不是 1）。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 0. 目录与文件名布局
# ---------------------------------------------------------------------------

SCREEN_SNAPSHOTS_DIRNAME = "screen_snapshots"

#: 进程内单调序号：snapshot_id 的确定性唯一性来源（同进程批量生成不碰撞）
_SNAPSHOT_SEQ = itertools.count(1)
SCREEN_TRACKING_DIRNAME = "screen_tracking"


def screen_snapshots_dir(storage_root: Path) -> Path:
    """storage_root 惯例 = storage/astock；快照目录与其平级。"""
    return Path(storage_root) / SCREEN_SNAPSHOTS_DIRNAME


def screen_tracking_dir(storage_root: Path) -> Path:
    return Path(storage_root) / SCREEN_TRACKING_DIRNAME


def snapshot_path(storage_root: Path, snapshot_id: str) -> Path:
    return screen_snapshots_dir(storage_root) / f"snap_{snapshot_id}.json"


def snapshot_index_path(storage_root: Path) -> Path:
    return screen_snapshots_dir(storage_root) / "index.json"


def track_path(
    storage_root: Path, snapshot_id: str, tracking_revision_id: str
) -> Path:
    return (
        screen_tracking_dir(storage_root)
        / f"track_{snapshot_id}_{tracking_revision_id}.json"
    )


def track_current_path(storage_root: Path, snapshot_id: str) -> Path:
    """当前采用版本指针（可变，仅存 revision 指向，不存计算产物）。"""
    return screen_tracking_dir(storage_root) / f"track_{snapshot_id}.current.json"


def track_state_path(storage_root: Path, task_key: str) -> Path:
    """运行状态记录：按任务身份（task_key）一任务一文件，互不覆盖。"""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(task_key))
    return screen_tracking_dir(storage_root) / f"state_{safe}.json"


def heavy_job_pending_path(storage_root: Path) -> Path:
    """heavy-job 抢锁失败的持久化待办（按任务身份管理）。"""
    return screen_tracking_dir(storage_root) / "heavy_job_pending.json"


# ---------------------------------------------------------------------------
# 1. 状态枚举（契约的唯一权威定义；服务层 import 使用，不得自造同义词）
# ---------------------------------------------------------------------------

# 快照来源
RUN_KINDS = ("weekly_chain", "backfill", "recompute")

# 快照规则范围（2026-09-16 新增「指定规则补算」）：
#   all    = 全部可执行规则（周五链 / 整周回填的常规形态）
#   subset = 仅指定若干规则（历史周单规则验证：全市场扫描成本与规则数
#            近似线性，实测单规则约为全量的 1/6~1/10，见 contract §11）
# 子集快照是**部分名单**：读取方（UI/导出）必须如实标注，否则会被误读成
# 「那周这些规则没有入选」。缺该字段的历史快照一律按 all 处理（向后兼容）。
RULES_SCOPE_ALL = "all"
RULES_SCOPE_SUBSET = "subset"
RULES_SCOPES = (RULES_SCOPE_ALL, RULES_SCOPE_SUBSET)

# 规则级质量状态：partial = 部分票评估失败（hit/miss 可用但非全量）
RULE_STATUS_OK = "ok"
RULE_STATUS_PARTIAL = "partial"
RULE_STATUS_ERROR = "error"
RULE_STATUSES = (RULE_STATUS_OK, RULE_STATUS_PARTIAL, RULE_STATUS_ERROR)

# 逐票状态（对一次"规则 × 股票"的评估结论）
TICKET_HIT = "hit"                      # 评估完成且命中
TICKET_MISS = "miss"                    # 评估完成、未命中
TICKET_ERROR = "error"                  # 该规则对该票计算失败
TICKET_NO_DATA = "no_data"              # asof 当日无 K 线（停牌/退市/未上市）
TICKET_NOT_IN_UNIVERSE = "not_in_universe"  # 不在快照股票池（picked 越界）
TICKET_STATUSES = (
    TICKET_HIT, TICKET_MISS, TICKET_ERROR, TICKET_NO_DATA, TICKET_NOT_IN_UNIVERSE,
)

# 跟踪完成状态机（运行状态；计算产物不可变，状态变化不产生新文件）
TRACK_COMPLETE = "complete"             # 本轮评估已结束（可含合法排除样本）
TRACK_PENDING = "pending"               # 窗口未结束或数据未齐 → 到期重试
TRACK_BLOCKED_BENCHMARK = "blocked_benchmark"  # 股票收益已成、基准缺 → 只补基准
TRACK_NO_TRADING_WEEK = "no_trading_week"     # 整周休市：合法终态，不顺延不重试
TRACK_FAILED = "failed"                 # 计算异常：记录失败，有界重试
TRACK_DATA_VERSION_CHANGED = "data_version_changed"  # 读版本前后不一致 → 中止重算
TRACK_TERMINAL_STATUSES = (TRACK_COMPLETE, TRACK_NO_TRADING_WEEK)

# 退出码语义（CLI/链尾段）
EXIT_OK = 0                # complete（含合法排除样本）或 no_trading_week
EXIT_RETRYABLE = 3         # pending / blocked_benchmark / data_version_changed
# 其余非零 = failed

# 成交性（首日开盘基准 = 成交假设；不承诺真实可成交）
FILL_OK = "ok"
FILL_LIMIT_UP_UNBUYABLE = "limit_up_unbuyable"  # 首日一字涨停（近似判定）
FILL_NO_BAR = "no_bar"                          # 无 K 线（停牌或缺数据）
FILL_UNKNOWN = "unknown"                        # 超出 limit_rules 边界/元数据不足
FILL_STATUSES = (FILL_OK, FILL_LIMIT_UP_UNBUYABLE, FILL_NO_BAR, FILL_UNKNOWN)

# 收益口径
RET_BASIS_SIGNAL = "signal_close"   # 信号收盘基准（理论口径）
RET_BASIS_OPEN = "week_first_open"  # 跟踪周首日开盘基准（成交假设口径）
RET_BASES = (RET_BASIS_SIGNAL, RET_BASIS_OPEN)
RET_BASIS_LABELS = {
    RET_BASIS_SIGNAL: "信号收盘基准收益",
    RET_BASIS_OPEN: "跟踪周首日开盘基准收益（成交假设）",
}

# ---------------------------------------------------------------------------
# 2. 快照身份：snapshot_id / content_fingerprint
# ---------------------------------------------------------------------------


def new_snapshot_id(*asof: int) -> str:
    """独立运行 ID：日期+毫秒+pid+进程内单调序号+随机后缀。

    唯一性保证分两层：
    - **进程内确定性**：单调序号（同进程同毫秒批量生成绝不重复）；
    - **跨进程**：pid + 随机后缀（uuid 只作补充，不依赖它保证唯一——
      6 位 hex 在批量生成下有可观测碰撞概率，实测已复现）。
    最终兜底仍是 O_EXCL 独占创建（契约 §0）。
    """
    stamp = time.strftime("%Y%m%d")
    ms = int(time.time() * 1000) % 1_000_000
    seq = next(_SNAPSHOT_SEQ)
    suffix = uuid.uuid4().hex[:6]
    parts = [f"{stamp}{ms:06d}", str(os.getpid()), f"{seq:04d}", suffix]
    if asof:
        parts.insert(0, str(int(asof[0])))
    return "_".join(parts)


def _stable(v: Any) -> Any:
    """dict/list 值规范化：嵌套结构排序后转稳定字符串（供指纹哈希）。"""
    if isinstance(v, dict):
        return json.dumps(
            {str(k): _stable(x) for k, x in sorted(v.items())},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    if isinstance(v, (list, tuple)):
        return json.dumps([_stable(x) for x in v], ensure_ascii=False, separators=(",", ":"))
    return v


def content_fingerprint(
    rule_fingerprints: Dict[str, str],
    universe_fingerprint: str,
    name_snapshot_id: str,
    data_version: Dict[str, Any],
    asof: int,
) -> str:
    """内容指纹（sha16）：同内容重跑可幂等提示"已有等价快照"。

    不含 generated_at / run_kind——语义是"算出的东西一样不一样"，
    而不是"哪次运行算的"。
    """
    payload = json.dumps(
        {
            "asof": int(asof),
            "rules": dict(sorted((str(k), str(v)) for k, v in rule_fingerprints.items())),
            "universe": str(universe_fingerprint),
            "name_snapshot": str(name_snapshot_id),
            "data_version": dict(sorted((str(k), _stable(v)) for k, v in data_version.items())),
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def create_snapshot_file_exclusive(path: Path, payload: Dict[str, Any]) -> Path:
    """O_EXCL 独占创建快照文件：已存在则抛 FileExistsError，绝不覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(path), flags)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except BaseException:
        try:
            os.unlink(str(path))
        except OSError:
            pass
        raise
    return path


# ---------------------------------------------------------------------------
# 3. week_id 与自然周对齐（契约 §2）
# ---------------------------------------------------------------------------


def _to_date(d: int) -> _date:
    return _date(d // 10000, (d // 100) % 100, d % 100)


def natural_week_window(
    calendar_dates: Sequence[int],
    signal_date: int,
) -> Dict[str, Any]:
    """给定交易日序列，返回信号周与跟踪周的窗口定义。

    返回 dict：
      signal_week:   [first, last] 信号自然周的首/末交易日
      track_week:    [first, last] 下一自然周的首/末交易日；整周休市为 None
      week_id:       信号周最后交易日（= signal_date）
      no_trading_week: bool 跟踪周整周休市
      short_week:    bool 任一端交易日 < 5（短周标记）
      anomaly:       str|None 用法错误（信号日非交易日 / 非周内最后交易日）

    判定规则（契约 §2）：
    - 自然周边界 = ISO 周（周一~周日 civil 区间），交易日按区间过滤。
    - 信号日必须是其 ISO 周内最后一个交易日；同周还有更晚交易日却把
      非最后日当锚点 → anomaly（fail-closed，不静默换锚）。
    - 跟踪周 = 下一 ISO 周 civil 区间内的交易日；区间内无交易日 →
      no_trading_week（合法终态，不顺延、不借用相邻周）。
    - 跨月/跨年周由 ISO 周历自然处理。
    """
    dates = sorted(int(d) for d in calendar_dates)
    signal = int(signal_date)
    if signal not in set(dates):
        return _week_anomaly("signal_date_not_trading_day", signal)

    sig_dt = _to_date(signal)
    iso_y, iso_w, _ = sig_dt.isocalendar()
    week_start = _date.fromisocalendar(int(iso_y), int(iso_w), 1)
    week_end = week_start + timedelta(days=6)

    in_week = [d for d in dates if week_start <= _to_date(d) <= week_end]
    if not in_week:
        return _week_anomaly("signal_week_has_no_trading_days", signal)
    if in_week[-1] != signal:
        return _week_anomaly("signal_date_not_week_last_trading_day", signal)

    tw_start = week_start + timedelta(days=7)
    tw_end = tw_start + timedelta(days=6)
    tw_dates = [d for d in dates if tw_start <= _to_date(d) <= tw_end]
    no_trading_week = not tw_dates
    short_week = len(in_week) < 5 or (not no_trading_week and len(tw_dates) < 5)

    return {
        "week_id": signal,
        "signal_week": [in_week[0], in_week[-1]],
        "track_week": ([tw_dates[0], tw_dates[-1]] if tw_dates else None),
        "track_week_dates": tw_dates,
        "no_trading_week": no_trading_week,
        "short_week": bool(short_week),
        "anomaly": None,
    }


def _week_anomaly(reason: str, signal: int) -> Dict[str, Any]:
    return {
        "week_id": int(signal),
        "signal_week": None,
        "track_week": None,
        "track_week_dates": [],
        "no_trading_week": False,
        "short_week": False,
        "anomaly": reason,
    }


# ---------------------------------------------------------------------------
# 4. 数据版本捕获（契约 §4）
# ---------------------------------------------------------------------------


def data_version_from_overlay_state(state: Any) -> Dict[str, Any]:
    """从 OverlayState 提取 data_version（复用项目已有身份，不自造）。

    覆盖 base/factor/supplement/delisted 的 dataset_id+manifest sha、
    delta 与 factor 的 watermark+commit_seq。基准指数面版本由调用方
    补 key="benchmark"（指数补数后股票面不变也必须使超额重算失效）。
    """
    return {
        "delta_store_id": str(getattr(state, "delta_store_id", "") or ""),
        "base_dataset_id": str(getattr(state, "base_dataset_id", "") or ""),
        "base_manifest_sha256": str(getattr(state, "base_manifest_sha256", "") or ""),
        "delisted_base_dataset_id": str(getattr(state, "delisted_base_dataset_id", "") or ""),
        "delisted_base_manifest_sha256": str(getattr(state, "delisted_base_manifest_sha256", "") or ""),
        "factor_base_dataset_id": str(getattr(state, "factor_base_dataset_id", "") or ""),
        "factor_base_manifest_sha256": str(getattr(state, "factor_base_manifest_sha256", "") or ""),
        "supplement_factor_dataset_id": str(getattr(state, "supplement_factor_base_dataset_id", "") or ""),
        "supplement_factor_manifest_sha256": str(getattr(state, "supplement_factor_base_manifest_sha256", "") or ""),
        "delta_watermark": int(getattr(state, "delta_watermark", 0) or 0),
        "delta_commit_seq": int(getattr(state, "delta_commit_seq", 0) or 0),
        "factor_watermark": int(getattr(state, "factor_watermark", 0) or 0),
        "factor_commit_seq": int(getattr(state, "factor_commit_seq", 0) or 0),
    }


def data_version_signature(data_version: Dict[str, Any]) -> str:
    """data_version 的比较签名（防同 cutoff 不同内容误判相等）。"""
    return hashlib.sha256(
        json.dumps(
            {str(k): _stable(v) for k, v in sorted(data_version.items())},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]


def tracking_revision_id(
    snapshot_id: str,
    bars_data_version: Dict[str, Any],
    benchmark_data_version: Dict[str, Any],
    algo_version: str,
    schema_version: str,
) -> str:
    """tracking_revision_id：绑定 快照+行情版本+基准版本+算法+schema。

    基准补数后基准版本变化 → 新 revision（旧产物保留不覆盖）；
    算法修正 bump algo_version → 新 revision（schema 不变也算新）。
    """
    payload = json.dumps(
        [
            str(snapshot_id),
            data_version_signature(bars_data_version),
            data_version_signature(benchmark_data_version),
            str(algo_version),
            str(schema_version),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 5. 分口径覆盖率（评审约束 1：分母 0 → null）
# ---------------------------------------------------------------------------


def coverage_by_basis(
    selected_count: int,
    valid_sig_count: int,
    valid_exec_count: int,
    excess_valid_count: int,
) -> Dict[str, Optional[float]]:
    """按口径分别记录覆盖率：有效样本 / 应评估样本（selected_count）。

    分母为 0 时该口径覆盖率返回 None——不是 0、不是 1，UI 不得把
    空仓周显示成"已结算 100%"。
    """
    def _rate(num: int, den: int) -> Optional[float]:
        if den <= 0:
            return None
        return round(num / den, 6)

    return {
        RET_BASIS_SIGNAL: _rate(valid_sig_count, selected_count),
        RET_BASIS_OPEN: _rate(valid_exec_count, selected_count),
        "excess": _rate(excess_valid_count, selected_count),
    }


# ---------------------------------------------------------------------------
# 6. 发布策略（契约 §6）
# ---------------------------------------------------------------------------

DEFAULT_NO_DATA_PUBLISH_MAX_RATIO = 0.05  # 5% 是策略默认值，不是质量结论


def snapshot_rules_scope(snapshot: Dict[str, Any]) -> str:
    """快照的规则范围（缺字段/取值非法 → all：旧快照语义不变）。"""
    scope = str((snapshot or {}).get("rules_scope") or RULES_SCOPE_ALL)
    return scope if scope in RULES_SCOPES else RULES_SCOPE_ALL


def scoped_rule_ids(snapshot: Dict[str, Any]) -> List[str]:
    """子集快照实际覆盖的规则 ID（all 快照返回空列表）。

    以 ``scoped_rule_ids`` 字段为准（发布时写入）；旧快照缺字段时按 rules
    逐条兜底，保证读取方拿到的"本周覆盖了哪些规则"永远与快照规则表一致。
    """
    if snapshot_rules_scope(snapshot) != RULES_SCOPE_SUBSET:
        return []
    ids = [str(r) for r in (snapshot or {}).get("scoped_rule_ids") or [] if str(r)]
    if ids:
        return ids
    return [str(r.get("rule_id")) for r in (snapshot or {}).get("rules") or []]


def subset_scope_notice(
    scope_ids: Sequence[str], rule_names: Optional[Sequence[str]] = None
) -> str:
    """子集快照的**单一文案来源**（L2 API 与导出 meta 共用，UI 不得另写一套）。

    语义必须说清两件事：① 本周只有这些规则跑过筛选；② 其余规则在本周
    **没有名单**，不等于它们当周没有入选——否则会被当成"该规则当周空仓"。
    """
    ids = [str(x) for x in (scope_ids or []) if str(x)]
    names = [str(x) for x in (rule_names or []) if str(x)]
    label = "、".join(names) if names else "、".join(ids)
    return (
        f"本周为「指定规则补算」：仅包含 {len(ids)} 条指定规则（{label}）；"
        "其他规则当周没有名单与跟踪结果，不代表这些规则当周没有入选。"
    )


@dataclass
class PublishPolicy:
    """发布门槛：partial/error 不自动发布；no_data 比例按阈值判定。

    threshold 是可配置策略默认值；产物必须记录实际阈值、缺失数量与
    判定结果，真实数据实测后再调整（评审约束 2）。
    """

    no_data_max_ratio: float = DEFAULT_NO_DATA_PUBLISH_MAX_RATIO

    def evaluate(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        rules = snapshot.get("rules") or []
        rule_statuses = {str(r.get("rule_id")): str(r.get("status")) for r in rules}
        has_non_publishable = any(
            s in (RULE_STATUS_PARTIAL, RULE_STATUS_ERROR) for s in rule_statuses.values()
        )
        universe_size = int(snapshot.get("universe_size") or 0)
        no_data_count = len(snapshot.get("no_data_codes") or [])
        ratio = (no_data_count / universe_size) if universe_size > 0 else None
        no_data_ok = ratio is None or ratio <= float(self.no_data_max_ratio)
        publishable = (not has_non_publishable) and no_data_ok
        return {
            "publishable": publishable,
            "partial_or_error_rules": sorted(
                k for k, s in rule_statuses.items()
                if s in (RULE_STATUS_PARTIAL, RULE_STATUS_ERROR)
            ),
            "no_data_count": no_data_count,
            "universe_size": universe_size,
            "no_data_ratio": (round(ratio, 6) if ratio is not None else None),
            "threshold_used": float(self.no_data_max_ratio),
            "verdict": (
                "publishable" if publishable
                else ("rule_partial_or_error" if has_non_publishable else "no_data_over_threshold")
            ),
        }


def load_week_index(storage_root: Path) -> Dict[str, Any]:
    p = snapshot_index_path(storage_root)
    if not p.exists():
        return {"schema_version": "1", "weeks": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": "1", "weeks": {}}


def publish_decision(
    storage_root: Path,
    week_id: int,
    snapshot_id: str,
    run_kind: str,
    *,
    rules_scope: str = RULES_SCOPE_ALL,
) -> Dict[str, Any]:
    """自动发布判定（只读，不改指针——指针更新仅在 publish_snapshot）。

    规则：
    - weekly_chain：该周尚无 published 指针时可发布。
    - backfill：仅在该周完全没有发布指针时补位（不替换已有指针，
      包括不替换手工发布的 recompute）。
    - recompute：永不自动发布（替换走显式 track-publish）。
    - 任何 run_kind：已有指针一律不自动替换（后台重试的新快照不改变
      正式名单，统计只计一次）。
    - rules_scope=subset（指定规则补算）：除"已有指针不替换"外再加一条
      **历史周护栏**——不得发布到最新发布周及之后。子集快照是部分名单，
      若占住周五链即将发布的当前周，链的全量快照会被"已有指针不替换"
      永久挡住，该周就只剩指定规则的数据了。
    """
    if run_kind not in RUN_KINDS:
        raise ValueError(f"unknown run_kind: {run_kind!r}")
    if rules_scope not in RULES_SCOPES:
        raise ValueError(f"unknown rules_scope: {rules_scope!r}")
    idx = load_week_index(storage_root)
    weeks = idx.get("weeks") or {}
    entry = weeks.get(str(int(week_id)))
    already = bool(entry and entry.get("published_snapshot_id"))
    if run_kind == "recompute":
        return {
            "publish": False,
            "reason": "recompute_never_auto_publish",
            "already_published": already,
        }
    if already:
        return {"publish": False, "reason": "already_published", "already_published": True}
    if rules_scope == RULES_SCOPE_SUBSET:
        latest = max((int(k) for k in weeks.keys()), default=0)
        if latest and int(week_id) >= latest:
            # 冷启动（latest=0，还没有任何发布周）不拦：无链条地盘可占，
            # 且 publish_decision 之后的发布仍受 already_published 保护
            return {
                "publish": False,
                "reason": "subset_scope_not_historical_week",
                "already_published": False,
                "latest_published_week": latest,
            }
        return {
            "publish": True,
            "reason": "subset_backfill_fills_empty_week",
            "already_published": False,
        }
    if run_kind == "backfill":
        return {"publish": True, "reason": "backfill_fills_empty_week", "already_published": False}
    return {"publish": True, "reason": "weekly_chain_first_publish", "already_published": False}


def _now_str() -> str:
    import datetime as _dt

    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def publish_snapshot(
    storage_root: Path,
    week_id: int,
    snapshot_id: str,
    run_kind: str,
    *,
    source: str = "auto",
) -> Dict[str, Any]:
    """更新发布指针（原子写 + 审计）。

    自动路径必须先通过 publish_decision；显式替换路径 source="manual"，
    允许覆盖既有指针但记录审计。周归属与发布门槛两条路径都校验
    （防把坏快照手工塞进正式名单）。
    """
    snap_file = snapshot_path(storage_root, snapshot_id)
    try:
        snap = json.loads(snap_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"snapshot 不存在: {snap_file}")
    if int(snap.get("week_id") or 0) != int(week_id):
        raise ValueError(
            f"快照周归属不符: snapshot week_id={snap.get('week_id')} 目标={week_id}"
        )
    verdict = PublishPolicy().evaluate(snap)
    if not verdict["publishable"]:
        raise ValueError(f"快照未过发布门槛: {verdict['verdict']}")

    idx = load_week_index(storage_root)
    weeks: Dict[str, Any] = idx.setdefault("weeks", {})
    prev = weeks.get(str(int(week_id)))
    weeks[str(int(week_id))] = {
        "published_snapshot_id": snapshot_id,
        "run_kind": run_kind,
        # 规则范围随索引冗余一份（读取方无需加载快照即可区分「全量周」与
        # 「指定规则补算周」并如实标注；快照内同名字段是权威来源）
        "rules_scope": snapshot_rules_scope(snap),
        "scoped_rule_ids": scoped_rule_ids(snap),
        "published_at": _now_str(),
        "published_by": source,
        "audit": {
            "previous_snapshot_id": (prev or {}).get("published_snapshot_id"),
            "previous_run_kind": (prev or {}).get("run_kind"),
            "previous_rules_scope": (prev or {}).get("rules_scope"),
            "replaced_at": _now_str(),
            "replaced_by": source,
        },
    }
    path = snapshot_index_path(storage_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))
    return weeks[str(int(week_id))]


# ---------------------------------------------------------------------------
# 7. 任务状态与 heavy-job 待办（评审约束 3：按任务身份管理）
# ---------------------------------------------------------------------------

# heavy-job 锁键：不复用 sync_lock 的 (root,source,adjustment,period) 键
HEAVY_JOB_LOCK_KEY = "heavy_job:screen_track"

# 抢锁失败后的有界退避（分钟）：不依赖重启、不等到下周五
HEAVY_JOB_RETRY_BACKOFF_MINUTES = (5, 15, 30)
HEAVY_JOB_MAX_ATTEMPTS = 4  # 首次 + 3 次退避

TASK_STATE_SCHEMA_VERSION = "1"


@dataclass
class TaskState:
    """运行状态记录（可变，一任务一文件；与不可变计算产物分离）。"""

    task_key: str
    completion: str = TRACK_PENDING
    attempts: int = 0
    last_attempt_at: Optional[str] = None
    last_error: Optional[str] = None
    tracking_revision_id: Optional[str] = None
    coverage: Dict[str, Optional[float]] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        payload = {
            "task_key": self.task_key,
            "completion": self.completion,
            "attempts": self.attempts,
            "last_attempt_at": self.last_attempt_at,
            "last_error": self.last_error,
            "tracking_revision_id": self.tracking_revision_id,
            "coverage": self.coverage,
            "schema_version": TASK_STATE_SCHEMA_VERSION,
        }
        payload.update(self.extra)
        return payload


def save_task_state(storage_root: Path, state: TaskState) -> Path:
    """任务状态原子写（每 task_key 一个文件，互不覆盖其他周的任务）。"""
    p = track_state_path(storage_root, state.task_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(state.to_payload(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(str(tmp), str(p))
    return p


def load_task_state(storage_root: Path, task_key: str) -> Optional[Dict[str, Any]]:
    p = track_state_path(storage_root, task_key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_pending_jobs(storage_root: Path) -> Dict[str, Any]:
    p = heavy_job_pending_path(storage_root)
    if not p.exists():
        return {"schema_version": "1", "jobs": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": "1", "jobs": {}}


def record_pending_job(
    storage_root: Path,
    task_key: str,
    *,
    reason: str,
    mark_exhausted: bool = False,
) -> Dict[str, Any]:
    """抢锁失败/未完成 → 写持久化待办（按 task_key 管理，同任务合并更新）。

    待办不是全局单条状态：不同周的任务各占一个 key；重试耗尽保留
    欠账并暴露失败原因（UI 手动补跑入口）。

    ``mark_exhausted=True``：不可重试的失败（配置类/计算异常）直接标欠账
    ——保留待办供人工处理，但不再进自动退避重试队列。
    """
    data = load_pending_jobs(storage_root)
    jobs: Dict[str, Any] = data.setdefault("jobs", {})
    prev = jobs.get(task_key) or {}
    attempts = int(prev.get("attempts") or 0) + 1
    exhausted = bool(mark_exhausted) or attempts >= HEAVY_JOB_MAX_ATTEMPTS
    idx = min(attempts - 1, len(HEAVY_JOB_RETRY_BACKOFF_MINUTES) - 1)
    backoff = (
        HEAVY_JOB_RETRY_BACKOFF_MINUTES[idx]
        if not exhausted and attempts - 1 < len(HEAVY_JOB_RETRY_BACKOFF_MINUTES)
        else None
    )
    jobs[task_key] = {
        "task_key": task_key,
        "reason": reason,
        "attempts": attempts,
        "recorded_at": _now_str(),
        "next_retry_in_minutes": backoff,
        "exhausted": exhausted,
        "last_error": prev.get("last_error"),
    }
    p = heavy_job_pending_path(storage_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(p))
    return jobs[task_key]


def clear_pending_job(storage_root: Path, task_key: str) -> None:
    data = load_pending_jobs(storage_root)
    jobs: Dict[str, Any] = data.setdefault("jobs", {})
    jobs.pop(task_key, None)
    p = heavy_job_pending_path(storage_root)
    if jobs:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(p))
    elif p.exists():
        os.unlink(str(p))  # 全清后不留空壳文件


# ---------------------------------------------------------------------------
# 8. 补偿判定（契约 §7：不看"文件是否存在"，看目标完成状态）
# ---------------------------------------------------------------------------


def compensation_required(
    *,
    has_product: bool,
    product_terminal: bool,
    revision_matches: bool,
    window_ended: bool,
) -> bool:
    """是否需要（重）算跟踪。

    触发 = 窗口已结束 且 满足任一：
      ① 无跟踪产物；② 产物非终态；③ 输入版本/算法版本与产物不一致
    （陈旧 complete 也被 ③ 捞回——修正"只查非终态"漏掉旧版本的缺陷）。
    窗口未结束 → 永不触发（pending 不是 missing，到期才算）。
    """
    if not window_ended:
        return False
    return (not has_product) or (not product_terminal) or (not revision_matches)


def should_complete_further(
    state: Optional[Dict[str, Any]],
    *,
    window_ended: bool,
    target_revision_id: str,
) -> bool:
    """补偿机制入口判定：合并状态文件与产物版本检查。

    no_trading_week 是日历推导的合法终态（无计算产物，算法/数据版本
    与它无关）→ 直接 False；complete 则要看 revision 是否仍然一致
    （算法升级会捞回重算）。
    """
    if not window_ended:
        return False
    if state is None:
        return True
    completion = str(state.get("completion") or "")
    if completion == TRACK_NO_TRADING_WEEK:
        return False
    if completion in TRACK_TERMINAL_STATUSES:
        return not (state.get("tracking_revision_id") == target_revision_id)
    return True
