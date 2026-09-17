# -*- coding: utf-8 -*-
"""跟踪「补算指定历史周」路由（2026-09-15 新增）。

用户场景：想知道某条规则在历史上某一周选出了什么、表现如何（例：指定
2026-07-31 的信号周，看 8/3~8/7 的实际收益）。这类周若当时没跑过链，就
没有发布快照，只读路由（api_routes/tracking.py）里根本不会出现。

实现 = 异步 CLI 子进程，复用既有命令与互斥机制：

- 单周精确补算：``track-weekly --week D --backfill 1``
  （CLI 以该周为锚点枚举 1 周：无发布快照先跑 review-weekly 补快照
  （run_kind=backfill），再结算跟踪；已有快照+产物则跳过，断点续跑）
- 最近 N 周：``track-weekly --backfill N``
- 指定规则（2026-09-16）：额外带 ``--rules A,B`` → 只补这几条规则
  （**子集快照** rules_scope=subset）。全市场扫描成本与规则数近似线性
  （实测单规则约为全量的 1/6~1/10），用于"验证某条规则在过去某周选出了
  什么"。两条护栏：
  ① 该周必须**完全没有发布指针**（已有全量周直接就能看单规则结果）；
  ② 该周必须**早于数据面最新信号周**（最新周归周五链——子集快照是部分
     名单，占住发布指针会让链的全量快照被"已有指针不替换"永久挡住）。
  另外子集复核 persist=False，绝不写 ``review_{asof}.json``（导出/筛选
  共享的全规则结果）。

为什么异步 + 子进程：
- 单周是分钟级重任务（全市场 5000+ 票扫描 + 跟踪结算；单规则约 8~11
  分钟），同步执行会把 Web 请求线程占满；
- 子进程与 serve 的 heavy-job 退避重试循环（api.py 的
  _auto_heavy_job_retry）跑同一套 CLI：CLI 内部抢全局重任务锁（锁被
  占用会记持久化待办、退出码 3），由既有重试循环稍后补跑——零新增互斥。

任务状态只存内存：重任务的**结果**落在快照/产物文件里（重启后可读），
内存里丢的只是"进度展示"；重跑由 CLI 的补偿判定（should_recompute）兜住。

安全：week 严格 8 位数字、rule_ids 必须是**当前可执行规则白名单**里的
ID（拼进命令行前的双重校验，杜绝参数注入与拼错规则静默产出空名单）；
weeks_back 限 1..52；同时最多一个 queued/running 任务（单 worker 顺序执行）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .context import ApiContext, get_ctx

router = APIRouter()

# 单次补算的周数上限：更大的整轮回填请在服务器上用命令行跑，避免 Web
# 入口发起十几分钟的重任务（与 CLI --backfill 的实际使用量级一致）
MAX_WEEKS_BACK = 52
# 单次子集补算的规则数上限：子集的意义是"快"（单规则约 8~11 分钟，
# 与规则数近似线性）；放开了就和整周回填没区别，还会让 Web 入口发起
# 不可控的重任务。需要更多规则请在服务器上用 CLI。
MAX_SUBSET_RULES = 5
# 每个任务保留的输出行数（UI 只展示尾部；完整日志由 CLI 侧承载）
_MAX_OUTPUT_LINES = 200
# 内存里保留的历史任务条数（运行中的不裁剪）
_MAX_JOBS_KEPT = 20


class TrackBackfillBody(BaseModel):
    week: Optional[str] = None        # "YYYYMMDD"：精确补算单周
    weeks_back: Optional[int] = None  # 最近 N 周（含最近发布周）
    rule_ids: Optional[List[str]] = None  # 只补这些规则（子集快照，须配 week）


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _project_root() -> Path:
    """仓库根：wtpy/apps/astock/api_routes/x.py → parents[4]。"""
    return Path(__file__).resolve().parents[4]


def _build_cmd(
    ctx: ApiContext,
    *,
    week: Optional[str],
    weeks_back: Optional[int],
    rule_ids: Optional[List[str]] = None,
) -> List[str]:
    """拼 CLI 命令。

    全局参数（--storage/--indicator-dir）必须在子命令**前**——这是 CLI 的
    既定语义（放子命令后不生效）；--storage 显式给仓库隔离的 storage_root，
    避免子进程落到默认目录。

    rule_ids 非空 → 追加 ``--rules a,b``：CLI 侧据此走子集快照
    （review-weekly --publish-scope subset，persist=False）。
    """
    cfg = ctx.cfg
    cmd: List[str] = [
        sys.executable, "-u", "-m", "wtpy.apps.astock",
        "--storage", str(cfg.storage_root),
    ]
    ind = getattr(cfg, "indicator_dir", None)
    if ind:
        cmd += ["--indicator-dir", str(ind)]
    cmd += ["track-weekly"]
    if week:
        # 锚定该周枚举 1 周：无快照先补快照再结算（= 精确补某一周）
        cmd += ["--week", week, "--backfill", "1"]
    else:
        cmd += ["--backfill", str(int(weeks_back or 0))]
    if rule_ids:
        cmd += ["--rules", ",".join(rule_ids)]
    return cmd


def _resolve_rule_ids(ctx: ApiContext, raw: Optional[List[str]]) -> Optional[List[str]]:
    """校验并归一请求的规则集（None = 全量规则）。

    白名单校验是**双重**的：API 层先挡（给用户清晰的 400），CLI 侧还会再
    校验一次（子进程是独立入口，不依赖上游拦干净）。规则 ID 来自注册表，
    拼进命令行前必须逐条确认可执行——拼错的规则名若被放行，会产出一份
    "该周什么都没有"的子集快照，用户会误以为规则当周确实无入选。
    """
    if raw is None:
        return None
    ids: List[str] = []
    for r in raw:
        s = str(r or "").strip()
        if s and s not in ids:
            ids.append(s)  # 去重保序：同一规则写两遍不跑两遍
    if not ids:
        raise HTTPException(400, "rule_ids 不能为空（不指定规则请留空表示全部规则）")
    if len(ids) > MAX_SUBSET_RULES:
        raise HTTPException(
            400,
            f"指定规则补算最多 {MAX_SUBSET_RULES} 条（收到 {len(ids)} 条）；"
            "更多规则请用整周补算或服务器命令行",
        )
    try:
        from ..service.screen_snapshots import list_screenable_rule_ids

        allowed = {str(r) for r in list_screenable_rule_ids(ctx.cfg)}
    except Exception as e:  # noqa: BLE001 — 规则目录不可用不能放行未校验的规则名
        raise HTTPException(503, f"规则目录不可用，无法校验规则：{e}") from e
    unknown = [r for r in ids if allowed and r not in allowed]
    if unknown:
        raise HTTPException(
            400,
            "规则不可执行或不存在：" + "、".join(unknown)
            + "（可筛选规则见 /api/v1/bagua/screen/rules）",
        )
    return ids


def _published_week_notice(ctx: ApiContext, week: str) -> Optional[str]:
    """**整周**补算遇到已发布周时的说明（None = 该周还没发布，可正常补算）。

    整周补算的产出会被 ``publish_decision`` 以 ``already_published`` 拒绝发布
    （跑了 60~90 分钟，页面数据一点没变），所以在入口就挡住，并把用户引到
    真正有用的动作：按规则追加（子集补算）。
    """
    from ..service import screen_contract as sc

    try:
        idx = sc.load_week_index(Path(ctx.cfg.storage_root))
        entry = (idx.get("weeks") or {}).get(str(int(week))) or {}
    except Exception:  # noqa: BLE001 — 索引读失败交给 CLI 侧兜底
        return None
    if not entry.get("published_snapshot_id"):
        return None
    scope = str(entry.get("rules_scope") or sc.RULES_SCOPE_ALL)
    scoped = [str(r) for r in (entry.get("scoped_rule_ids") or [])]
    who = (
        "该周是「指定规则补算」周，名单只含 " + "、".join(scoped)
        if scope == sc.RULES_SCOPE_SUBSET and scoped
        else ("该周已有全量发布名单" if scope != sc.RULES_SCOPE_SUBSET else "该周已有部分规则名单")
    )
    return (
        f"{week} 已有发布快照（来源={entry.get('run_kind') or '—'}；{who}）。"
        "整周补算不会替换已发布名单（跑了也不会生效），因此未受理。"
        "要补某条规则请用「指定规则」方式——它会把该规则追加进这一周的名单；"
        "要重算整周请用 CLI：先 review-weekly 再 track-publish 显式转正。"
    )


def _subset_scope_hint(
    ctx: ApiContext, week: str, rule_ids: Optional[List[str]] = None
) -> Optional[str]:
    """指定规则补算的护栏；返回错误文案（None = 受理）。

    2026-09-16 用户口径修正：**补算单位是「周 × 规则」**。一条规则补过某周，
    只说明这条规则那一周有数据了，**不代表这一周归它**——别的规则同样要能补
    这一周。所以该周已有名单时：
    - 请求的规则**都已在该周名单里** → 无需补算（提示，不白扫）；
    - 还有规则没覆盖 → 受理"追加"（只扫缺的规则，合并进该周名单）。
    唯一保留的边界是数据面最新信号周（归周五链，见 ②）。
    """
    from ..service import screen_contract as sc
    from ..service import screen_tracking as tracksvc

    try:
        idx = sc.load_week_index(Path(ctx.cfg.storage_root))
        entry = (idx.get("weeks") or {}).get(str(int(week))) or {}
    except Exception:  # noqa: BLE001 — 索引读失败交给 CLI 侧兜底
        entry = {}
    if entry.get("published_snapshot_id"):
        want = [str(r) for r in (rule_ids or [])]
        snap = None
        try:
            from ..service.screen_snapshots import load_published_snapshot_for_week

            snap = load_published_snapshot_for_week(ctx.cfg, int(week))
        except Exception:  # noqa: BLE001 — 读不到就当"都能追加"，由 CLI 侧定夺
            snap = None
        have = {str(r.get("rule_id")) for r in (snap or {}).get("rules") or []}
        if snap is not None and want and all(r in have for r in want):
            scope = str(entry.get("rules_scope") or sc.RULES_SCOPE_ALL)
            extra = (
                "（该周是全量周，本来就包含这条规则）"
                if scope != sc.RULES_SCOPE_SUBSET
                else ""
            )
            return (
                f"{week} 的名单里已经有：{'、'.join(want)}{extra}，无需补算——"
                "直接点该周的「周明细」即可。"
                "如需用新数据重算，请用 CLI track-publish 显式转正（会记录审计）。"
            )
    # ② 该周必须早于数据面最新信号周（最新周归周五链）
    try:
        latest = tracksvc.latest_signal_week(ctx.cfg)
    except Exception:  # noqa: BLE001 — 推不出来就交给契约层护栏兜底
        latest = 0
    if latest and int(week) >= int(latest):
        return (
            f"{week} 是数据面最新信号周（{latest}）或更晚：最新的周归周五链，"
            "指定规则补算只支持更早的历史周。"
        )
    return None


def _week_tracking_ready(cfg, week: str, entry: dict) -> bool:
    """该周当前发布快照是否已有跟踪产物（结算过）。

    整周补算在"已有名单"的周上不会重新发布（``already_published``），但它仍然会
    **结算**该周的跟踪——所以只有"已发布且已结算"的周才是真的白跑。追加规则之后
    的周（名单变了、新快照还没结算）恰恰需要这个入口，不能一起挡。
    """
    try:
        from ..service import screen_contract as sc

        root = Path(cfg.storage_root)
        snap_id = str(entry.get("published_snapshot_id") or "")
        if not snap_id:
            return False
        cur_path = sc.track_current_path(root, snap_id)
        if not cur_path.exists():
            return False
        cur = json.loads(cur_path.read_text(encoding="utf-8")) or {}
        rev = str(cur.get("tracking_revision_id") or "")
        if not rev:
            return False
        return sc.track_path(root, snap_id, rev).exists()
    except Exception:  # noqa: BLE001 — 判定不了就按"没结算"放行（宁可多跑一次）
        return False


def _week_backfill_hint(ctx: ApiContext, week: str) -> Optional[str]:
    """整周（全部规则）补算的前置校验；返回错误文案（None = 受理）。

    2026-09-16 补：整周补算在"已发布且已结算"的周上是白跑（跑 60~90 分钟全市场
    扫描，最后 ``publish_decision=already_published`` 不发布，页面数据一点没变），
    在入口就挡住并指向"按规则追加"。但**已发布未结算**的周必须放行——那正是
    结算入口（追加规则后就走这条）。
    """
    from ..service import screen_contract as sc

    try:
        idx = sc.load_week_index(Path(ctx.cfg.storage_root))
        entry = (idx.get("weeks") or {}).get(str(int(week))) or {}
    except Exception:  # noqa: BLE001
        entry = {}
    if not entry.get("published_snapshot_id"):
        return None
    if not _week_tracking_ready(ctx.cfg, week, entry):
        return None  # 名单在、跟踪还没结算 → 放行（会跳过筛选直接结算）
    return _published_week_notice(ctx, week)


def _run_backfill_job(ctx: ApiContext, job: Dict[str, Any]) -> None:
    """执行一个补算任务（供 worker 调用）：跑子进程并记录退出码语义。"""
    cfg = ctx.cfg
    lock = ctx.track_backfill_lock
    cmd = job.get("_cmd") or []
    rc = -1
    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            env["MARKET_DATA_ROOT"] = str(cfg.market_data_root)
            if getattr(cfg, "astock_env", None):
                env["ASTOCK_ENV"] = str(cfg.astock_env)
        except Exception:  # noqa: BLE001 — 环境变量缺失不阻塞（CLI 有默认值）
            pass
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(_project_root()),
            env=env,
        )
        for line in proc.stdout:  # type: ignore[union-attr]
            with lock:
                out = job.setdefault("output", [])
                out.append(line.rstrip())
                if len(out) > _MAX_OUTPUT_LINES:
                    del out[: len(out) - _MAX_OUTPUT_LINES]
        rc = proc.wait()
    except Exception as e:  # noqa: BLE001 — 子进程起不来要如实记录，不能吞
        with lock:
            job.setdefault("output", []).append(f"[ERROR] 子进程执行失败: {e}")
        rc = -2
    with lock:
        job["exit_code"] = rc
        job["finished_at"] = _now()
        # 退出码语义（CLI track_exit_code_for_completion）：0=完成；
        # 2=配置/快照类可读错误；3=可重试（heavy-job 锁被占用或数据版本
        # 变化，CLI 已记待办，serve 的重试循环稍后自动补跑）
        job["status"] = "done" if rc == 0 else ("retryable" if rc == 3 else "failed")


def _track_backfill_worker(ctx: ApiContext) -> None:
    """单 worker：顺序跑完所有 queued 任务（防连点并发全市场扫描）。"""
    try:
        while True:
            with ctx.track_backfill_lock:
                job = next(
                    (j for j in ctx.track_backfill_jobs.values() if j["status"] == "queued"),
                    None,
                )
                if job is None:
                    return
                job["status"] = "running"
                job["started_at"] = _now()
            _run_backfill_job(ctx, job)
    finally:
        # 无论正常结束还是异常：复位标志，下一次投递能重新拉起 worker
        with ctx.track_backfill_lock:
            ctx.track_backfill_worker_started = False


def _signal_day_hint(ctx: ApiContext, week: str) -> Optional[str]:
    """校验信号日语义；返回错误文案（None = 通过）。

    用户会按「指定某一天」理解这个入口（例：想补 2026-06-26），但跟踪的
    信号日是**该 ISO 周最后一个交易日**（通常周五；节假日周是周四）。
    放行周中日期会让全市场复核白跑两小时才在 CLI 侧 fail-closed，必须
    在入口挡住，并直接给出该周应有的日期。
    """
    try:
        from datetime import date as _date
        from datetime import timedelta

        from ..service import screen_contract as sc
        from ..service import screen_tracking as tracksvc

        cal = tracksvc._load_calendar_or_none(ctx.cfg)
    except Exception:  # noqa: BLE001 — 日历不可用则不拦（CLI 侧仍会 fail-closed）
        return None
    if cal is None or not cal.dates:
        return None
    dates = sorted(int(x) for x in cal.dates)
    d = int(week)
    win = sc.natural_week_window(dates, d)
    if not win.get("anomaly"):
        return None
    sig = _date(d // 10000, (d // 100) % 100, d % 100)
    iso = sig.isocalendar()
    ws = _date.fromisocalendar(int(iso[0]), int(iso[1]), 1)
    we = ws + timedelta(days=6)
    in_week = [
        x for x in dates
        if ws <= _date(x // 10000, (x // 100) % 100, x % 100) <= we
    ]
    hint = (
        f"该周最后一个交易日是 {max(in_week)}，请改填它"
        if in_week else "该周没有任何交易日"
    )
    if win["anomaly"] == "signal_date_not_trading_day":
        return f"{week} 不是交易日；{hint}。"
    if win["anomaly"] == "signal_date_not_week_last_trading_day":
        return f"{week} 不是该周最后一个交易日；{hint}（跟踪以每周最后交易日为信号日）。"
    return f"{week} 不满足信号日语义（{win['anomaly']}）；{hint}。"


@router.post("/api/v1/bagua/track/backfill")
def api_track_backfill(
    payload: TrackBackfillBody, ctx: ApiContext = Depends(get_ctx)
) -> dict:
    """提交补算任务（异步）。返回 job_id，进度查 status 端点。

    payload 三选一组合：
      {week}                    → 精确补某一周（全部规则）
      {weeks_back}              → 最近 N 周（全部规则）
      {week, rule_ids}          → 只补指定规则（子集快照，历史周专用）
    """
    raw_week = (payload.week or "").strip()
    week: Optional[str] = None
    if raw_week:
        s = raw_week.replace("-", "").replace("/", "")
        if not re.fullmatch(r"\d{8}", s):
            raise HTTPException(400, "week 需为 8 位日期（YYYYMMDD），例如 20260731")
        week = s
    wb = payload.weeks_back
    if week is None and wb is None:
        raise HTTPException(400, "必须指定 week（精确补某一周）或 weeks_back（最近 N 周）")
    if week is not None and wb is not None:
        raise HTTPException(400, "week 与 weeks_back 只能二选一")
    if wb is not None and (int(wb) < 1 or int(wb) > MAX_WEEKS_BACK):
        raise HTTPException(400, f"weeks_back 必须在 1..{MAX_WEEKS_BACK} 之间")
    rule_ids = _resolve_rule_ids(ctx, payload.rule_ids)
    if rule_ids and week is None:
        # 批量（weeks_back）不支持规则范围：其锚点是最近发布周，那几周
        # 早已有发布指针，子集补算逐周都会被护栏拒绝——直接拒绝比让用户
        # 等一轮再说"全跳过"更清楚。
        raise HTTPException(
            400, "指定规则补算需要同时给出 week（一次补一个历史周）；"
                 "批量补算不支持指定规则"
        )
    if week is not None:
        hint = _signal_day_hint(ctx, week)
        if hint:
            raise HTTPException(400, hint)
    if rule_ids and week is not None:
        hint = _subset_scope_hint(ctx, week, rule_ids)
        if hint:
            raise HTTPException(400, hint)
    elif week is not None:
        # 整周（全部规则）补算：该周已有发布指针时同样是白跑（不会发布）
        hint = _week_backfill_hint(ctx, week)
        if hint:
            raise HTTPException(400, hint)

    with ctx.track_backfill_lock:
        for j in ctx.track_backfill_jobs.values():
            if (
                j["status"] in ("queued", "running")
                and j.get("week") == week
                and j.get("weeks_back") == wb
                and list(j.get("rule_ids") or []) == list(rule_ids or [])
            ):
                raise HTTPException(
                    409, f"相同的补算任务已在进行中（job {j['job_id']}）"
                )
        job_id = uuid.uuid4().hex[:12]
        job: Dict[str, Any] = {
            "job_id": job_id,
            "week": week,
            "weeks_back": int(wb) if wb is not None else None,
            "rule_ids": list(rule_ids or []),
            "status": "queued",
            "queued_at": _now(),
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "output": [],
            "_cmd": _build_cmd(
                ctx, week=week, weeks_back=wb, rule_ids=rule_ids
            ),
        }
        ctx.track_backfill_jobs[job_id] = job
        # 裁剪旧记录（只裁已结束的，运行中/排队中的必须保留）
        if len(ctx.track_backfill_jobs) > _MAX_JOBS_KEPT:
            finished = sorted(
                (
                    j for j in ctx.track_backfill_jobs.values()
                    if j["status"] not in ("queued", "running")
                ),
                key=lambda j: str(j.get("queued_at") or ""),
            )
            drop = len(ctx.track_backfill_jobs) - _MAX_JOBS_KEPT
            for j in finished[: max(0, drop)]:
                ctx.track_backfill_jobs.pop(j["job_id"], None)
        start_worker = not ctx.track_backfill_worker_started
        if start_worker:
            ctx.track_backfill_worker_started = True
    if start_worker:
        threading.Thread(
            target=_track_backfill_worker, args=(ctx,), daemon=True
        ).start()
    if rule_ids:
        target = f"{week} 仅 {len(rule_ids)} 条指定规则"
        message = (
            f"已提交指定规则补算（{target}）：单条约 8~11 分钟（全市场扫描+结算）。"
            "该周只有这几条规则有名单与跟踪结果（其他规则当周无数据，"
            "不代表它们当周无入选）；数据为按当前规则重建，不代表当时实际发布名单。"
        )
    else:
        target = week if week else f"最近 {wb} 周"
        message = (
            f"已提交补算（{target}）：全规则历史重建属于全市场重任务，耗时通常约 60~90+ 分钟/周；"
            "实际时长受规则数、股票池规模和机器性能影响，任务将在后台执行。"
            "数据为按当前规则历史重建，不代表当时实际发布名单。"
        )
    return {
        "ok": True,
        "job_id": job_id,
        "week": week,
        "weeks_back": job["weeks_back"],
        "rule_ids": job["rule_ids"],
        "status": "queued",
        "message": message,
    }


@router.get("/api/v1/bagua/track/published-weeks")
def api_track_published_weeks(
    weeks: int = 26,
    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    """已发布信号周清单（倒序，最多 ``weeks`` 个）：补算入口的"哪些周不能再补"。

    为什么需要它（2026-09-16 用户实操反馈）：补算护栏是**按周**判定的——某周一旦
    有发布指针，**任何规则**的自动补算都不会改动它，跟用户选的那条规则有没有数据
    无关。页面上原先看不出这一点，用户只能在提交后收到 400，还会被报错里的规则名
    搞糊涂（那是"占住这一周"的规则，不是他选的那条）。抽屉据本接口提前提示。
    """
    from ..service import screen_contract as sc

    if weeks < 1 or weeks > 104:
        weeks = 26
    try:
        idx = sc.load_week_index(Path(ctx.cfg.storage_root))
        entries = idx.get("weeks") or {}
    except Exception:  # noqa: BLE001 — 索引不可读时返回空清单（前端退回"不提示"）
        entries = {}
    out = []
    for k in sorted((int(x) for x in entries.keys()), reverse=True)[:weeks]:
        e = entries.get(str(k)) or {}
        out.append(
            {
                "week_id": k,
                "published_snapshot_id": e.get("published_snapshot_id"),
                "run_kind": e.get("run_kind"),
                "rules_scope": str(e.get("rules_scope") or sc.RULES_SCOPE_ALL),
                "scoped_rule_ids": [str(x) for x in (e.get("scoped_rule_ids") or [])],
                "published_at": e.get("published_at"),
                # 是否已结算跟踪：没结算的周允许用"整周补算"触发结算
                # （追加规则之后就走这条），已结算的整周补算才是白跑
                "tracking_ready": _week_tracking_ready(ctx.cfg, str(k), e),
            }
        )
    return {"ok": True, "count": len(out), "weeks": out}


@router.get("/api/v1/bagua/track/backfill/status")
def api_track_backfill_status(ctx: ApiContext = Depends(get_ctx)) -> dict:
    """补算任务状态（新→旧）。运行中任务的 output 是 CLI 输出尾部。"""
    with ctx.track_backfill_lock:
        jobs = [
            {k: v for k, v in j.items() if not k.startswith("_")}
            for j in sorted(
                ctx.track_backfill_jobs.values(),
                key=lambda x: str(x.get("queued_at") or ""),
                reverse=True,
            )
        ]
    running = [j for j in jobs if j["status"] in ("queued", "running")]
    return {"ok": True, "running": len(running), "jobs": jobs}
