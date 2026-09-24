"""FastAPI server for A-stock frontend.

Route handlers live in :mod:`wtpy.apps.astock.api_routes`; this module only
assembles the app: shared context (services + sync/export state), routers and
static mount. See ``serve()`` for the CLI entry point.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import AStockConfig, get_default_config
from .forecast.service import ForecastService
from .service.backtest import BacktestService
from .service.db import get_app_setting
from .service.jobs import JobStore, resolve_bt_max_workers
from .service.rules import RuleService
from .version import get_version_string
from .api_routes import (
    ApiContext,
    backtests,
    bagua,
    bagua_workbench,
    experiments,
    forecast,
    research,
    rules,
    system,
    track_backfill,
    tracking,
)

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

# Backward-compatible re-exports: request models previously lived here.
from .api_routes.backtests import BacktestBody  # noqa: E402
from .api_routes.bagua import BaguaBatchBody, BaguaExportBody  # noqa: E402
from .api_routes.forecast import ForecastBatchBody  # noqa: E402
from .api_routes.rules import RuleCreate, RuleUpdate, RuleValidate  # noqa: E402
from .api_routes.system import SyncStartBody  # noqa: E402

_ALL_ROUTERS = (
    rules.router,
    backtests.router,
    experiments.router,
    research.router,
    forecast.router,
    bagua.router,
    bagua_workbench.router,
    system.router,
    tracking.router,
    track_backfill.router,
)

# Shared cross-thread lock for eod_sync_state.json writes: the EOD watcher
# thread and the CA auto-sync thread both persist to the same state file
# (read-modify-write without a lock could lose updates or corrupt the JSON).
_EOD_STATE_LOCK = threading.Lock()

# Small JSON endpoints (/api/v1/rules) keep a tight cap (import has its own
# 512KB service check); all other paths get a loose app-wide abuse guard so
# xlsx / multipart uploads (weekly reports can exceed 2MB) are not rejected.
_MAX_RULES_BODY_BYTES = 2 * 1024 * 1024
_MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024
_LARGE_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


def _body_size_limit(path: str) -> int:
    if path.startswith("/api/v1/rules"):
        return _MAX_RULES_BODY_BYTES
    return _MAX_REQUEST_BODY_BYTES


def eod_sync_day_gate(
    *,
    now,
    sync_time: str = "18:30",
    sync_weekday: int = 4,
    schedule_mode: str = "weekday",
    trading_days=None,
) -> tuple:
    """日期/时刻门（不看数据新鲜度），返回 ``(eligible, reason, effective_mode)``。

    与 :func:`eod_sync_decide` 共用同一套口径；调度线程先用它低成本判断
    「今天是不是候选日」，通过后再算昂贵的全链路 lag。

    ``schedule_mode``：
    - ``weekday``（旧行为）：固定每周 ``sync_weekday`` 触发；
    - ``last_trading_day``：本周**最后一个交易日**触发——节假日前移时自动
      提前（如 2026-09-25 中秋休市，则本周触发日 = 9-24 周四）。需要
      ``trading_days``（前瞻交易日历，含未来开市日）；日历缺失或不覆盖
      本周日时退化为 ``weekday`` 规则（effective_mode 返回实际采用值），
      绝不静默停更。
    """
    from .service import eod_schedule as _sch

    mode = _sch.normalize_schedule_mode(schedule_mode, default=_sch.SCHEDULE_WEEKDAY)
    if mode == _sch.SCHEDULE_LAST_TRADING_DAY and trading_days:
        today_key = int(now.strftime("%Y%m%d"))
        verdict = _sch.is_last_trading_day_of_week(trading_days, today_key)
        if verdict is False:
            last = _sch.week_last_trading_day(trading_days, today_key)
            if last is None:
                return (
                    False,
                    "本周全周休市（按前瞻交易日历），跳过",
                    _sch.SCHEDULE_LAST_TRADING_DAY,
                )
            return (
                False,
                f"今日非本周最后交易日（本周末交易日={last}）",
                _sch.SCHEDULE_LAST_TRADING_DAY,
            )
        if verdict is None:
            # 日历覆盖不到本周日：退化为固定周几规则（文档承诺的兜底）。
            mode = _sch.SCHEDULE_WEEKDAY
    else:
        mode = _sch.SCHEDULE_WEEKDAY

    if mode == _sch.SCHEDULE_WEEKDAY and now.weekday() != sync_weekday:
        return False, f"非计划更新日（weekday={sync_weekday}）", mode
    if now.strftime("%H:%M") < sync_time:
        return False, f"未到自动同步时间（{sync_time}）", mode
    return True, "", mode


def eod_sync_decide(
    *,
    lag: Optional[int],
    now,
    sync_time: str = "18:30",
    sync_weekday: int = 4,
    min_lag: int = 1,
    last_trigger_day=None,
    schedule_mode: str = "weekday",
    trading_days=None,
) -> tuple:
    """Decide whether an automatic EOD sync should fire.

    Returns ``(trigger: bool, reason: str, today_key)`` where ``today_key``
    is the date the caller should remember as the last trigger day (None when
    not triggering). Weekends, pre-``sync_time`` hours, unknown lag, a lag
    below ``min_lag`` and an already-triggered day all short-circuit to False.
    日期/时刻门口径见 :func:`eod_sync_day_gate`（含 last_trading_day 模式
    的前瞻日历判定与退化规则）。
    """
    import datetime as _dt

    from .service import eod_schedule as _sch

    eligible, reason, mode = eod_sync_day_gate(
        now=now,
        sync_time=sync_time,
        sync_weekday=sync_weekday,
        schedule_mode=schedule_mode,
        trading_days=trading_days,
    )
    if not eligible:
        return False, reason, None
    today = now.date()
    if last_trigger_day is not None and last_trigger_day == today:
        return False, "今日已触发过自动同步", None
    if lag is None:
        return False, "无法判断数据新鲜度（跳过）", None
    if lag < min_lag:
        return False, f"数据已最新（lag={lag}）", None
    basis = (
        "本周最后交易日"
        if mode == _sch.SCHEDULE_LAST_TRADING_DAY
        else f"周历日（weekday={sync_weekday}）"
    )
    return True, f"{basis}，raw 数据滞后 {lag} 个交易日", today


def _effective_data_lag(health: dict) -> Optional[int]:
    """Whole-chain data lag, not just raw.

    ``tushare_product_data_health`` returns a ``trading_day_lag`` map plus
    ``formal_l1``/``formal_l2`` surfaces. The EOD scheduler must treat the
    whole product chain as "fresh" only when raw AND factor AND the formal
    L1/L2 pair are all current: raw can be fresh while factor hit a rate
    limit (partial) and the formal pair silently stayed on an old date —
    in that case the retry must still fire (2026-08-13 事故根因)。
    """
    tl = health.get("trading_day_lag") or {}
    lag = tl.get("raw")
    factor_lag = tl.get("factor")
    if factor_lag is not None:
        lag = max(lag or 0, factor_lag)
    expected = health.get("expected_latest_trading_day")
    if expected:
        formal_max = None
        for key in ("formal_l2", "formal_l1"):
            md = (health.get(key) or {}).get("max_date")
            if md:
                formal_max = md if formal_max is None else max(formal_max, int(md))
        if formal_max is not None and int(formal_max) < int(expected):
            # 正式 L1/L2 落后于预期交易日:至少记为 1 个交易日滞后,
            # 足以让 eod_sync_decide 触发重试(精确 lag 值对重试无影响)。
            lag = max(lag or 0, 1)
    return lag


def _auto_eod_sync(cfg: AStockConfig, ctx: "ApiContext") -> None:
    """Startup + scheduled EOD auto-sync of Tushare market data.

    Scheduling: by default it fires on the **last trading day of each week**
    (``ASTOCK_EOD_SYNC_SCHEDULE=last_trading_day``, 2026-09 起默认——周界
    由 Tushare 前瞻交易日历判定，节假日前移自动提前，如中秋节前周四）；
    ``ASTOCK_EOD_SYNC_SCHEDULE=weekday`` 退回旧的固定周几口径
    （``ASTOCK_EOD_SYNC_WEEKDAY``, Friday by default)。前瞻日历不可用时
    同样退化为固定周几，绝不静默停更。触发时刻
    (``ASTOCK_EOD_SYNC_TIME``) 之后检查新鲜度，滞后则 spawn 与 UI 按钮相同
    的增量同步（``--source tushare --mode incremental``）加 ``--fresh``。如果
    数据尚未滞后（Tushare 发布晚）每 ``ASTOCK_EOD_SYNC_POLL_SECONDS``
    （默认 30 分钟）重试；当天触发成功后睡到下一个候选日。

    last_trading_day 模式下调度线程每个工作日（周一~周五）在 sync_time
    醒来一次做日历判定，成本是一次本地 JSON 读取；真正的数据健康检查只
    在「今日 = 本周最后交易日」候选判定通过后才执行。

    The trigger record is persisted to ``storage/astock/eod_sync_state.json``
    so the UI can show "上次自动同步时间".

    Env switches (all optional):
      ASTOCK_EOD_SYNC_ENABLED=0|1        (default 1)
      ASTOCK_EOD_SYNC_INDEX_ETF=0|1      (default 1: 股票链后顺序执行
                                         指数/ETF 增量同步)
      ASTOCK_EOD_SYNC_INCLUDE_BSE=0|1    (default 1: 股票链票池含北交所，
                                         新票（含首批北交所）拉全历史入库)
      ASTOCK_EOD_SYNC_STARTUP=0|1        (default 1, run once on startup)
      ASTOCK_EOD_SYNC_TIME=HH:MM         (default 18:30)
      ASTOCK_EOD_SYNC_SCHEDULE=last_trading_day|weekday
                                         (default last_trading_day)
      ASTOCK_EOD_SYNC_WEEKDAY=0..6       (default 4: Friday；schedule=weekday
                                         或日历退化兜底时生效)
      ASTOCK_EOD_SYNC_MIN_LAG_DAYS=N     (default 1)
      ASTOCK_EOD_SYNC_POLL_SECONDS=N     (default 1800, min 60)
      ASTOCK_EOD_SYNC_MAX_RETRIES=N      (default 2, same-day retries after
                                         a failed run, poll_seconds apart)
      ASTOCK_EOD_AUTO_EXPORT_ENABLED=0|1 (default 1: 链尾自动生成全市场
                                         数据表供前端下载)
    """
    import datetime as _dt
    import json as _json
    import os as _os
    import threading as _thr
    import time as _time

    def _env_flag(name: str, default: str = "1") -> bool:
        return _os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")

    if not _env_flag("ASTOCK_EOD_SYNC_ENABLED", "1"):
        print("[EOD_SYNC] 已禁用（ASTOCK_EOD_SYNC_ENABLED=0），跳过自动更新")
        return
    if not cfg.market_data_root.exists():
        # 首次部署时仓库根可能尚未创建:先尝试创建(ensure_dirs 已覆盖,
        # 这里双保险),创建失败才跳过——否则自动同步会静默失效。
        try:
            cfg.market_data_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"[EOD_SYNC] 数据目录创建失败（{cfg.market_data_root}）: {e}，跳过自动更新")
            return

    sync_time = _os.environ.get("ASTOCK_EOD_SYNC_TIME", "18:30")
    try:
        sync_weekday = min(
            6, max(0, int(_os.environ.get("ASTOCK_EOD_SYNC_WEEKDAY", "4")))
        )
    except ValueError:
        sync_weekday = 4
    from .service import eod_schedule as _sch

    schedule_mode = _sch.normalize_schedule_mode(
        _os.environ.get("ASTOCK_EOD_SYNC_SCHEDULE"),
        default=_sch.SCHEDULE_LAST_TRADING_DAY,
    )
    try:
        min_lag = max(0, int(_os.environ.get("ASTOCK_EOD_SYNC_MIN_LAG_DAYS", "1")))
    except ValueError:
        min_lag = 1
    try:
        # retry interval AFTER the sync time when data is not yet lagged
        poll_sec = max(60, int(_os.environ.get("ASTOCK_EOD_SYNC_POLL_SECONDS", "1800")))
    except ValueError:
        poll_sec = 1800
    try:
        # same-day retries after a failed run (0 = never retry)
        max_retries = max(0, int(_os.environ.get("ASTOCK_EOD_SYNC_MAX_RETRIES", "2")))
    except ValueError:
        max_retries = 2
    startup_check = _env_flag("ASTOCK_EOD_SYNC_STARTUP", "1")

    # persisted trigger record for the UI status card
    # (ASTOCK_EOD_STATE_PATH overrides the default repo path — tests use it
    # to keep the real state file untouched)
    state_path_env = _os.environ.get("ASTOCK_EOD_STATE_PATH")
    state_path = Path(state_path_env) if state_path_env else (
        Path(__file__).resolve().parents[3] / "storage" / "astock" / "eod_sync_state.json"
    )

    # Watcher threads set this event when a spawned child exits, so the
    # scheduler wakes up exactly then instead of sleeping to the next day.
    wake_event = _thr.Event()

    last_trigger_day = None

    def _load_state() -> Dict[str, Any]:
        try:
            if state_path.exists():
                return _json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_state(extra: Optional[Dict[str, Any]] = None) -> None:
        try:
            with _EOD_STATE_LOCK:
                st = _load_state()
                st.update({
                    "enabled": True,
                    "sync_time": sync_time,
                    "sync_weekday": sync_weekday,
                    "schedule_mode": schedule_mode,
                    "schedule_text": _sch.schedule_mode_label(
                        schedule_mode, sync_weekday, sync_time
                    ),
                    "min_lag_days": min_lag,
                    "poll_seconds": poll_sec,
                    "updated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                if extra:
                    st.update(extra)
                state_path.parent.mkdir(parents=True, exist_ok=True)
                _tmp = state_path.with_suffix(".json.tmp")
                _tmp.write_text(
                    _json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                _tmp.replace(state_path)  # atomic: readers never see a torn file
        except Exception:
            pass

    def _lag_days() -> Optional[int]:
        from .data.dataset_store import DatasetStore
        from .data.tushare_product import tushare_product_data_health

        health = tushare_product_data_health(DatasetStore(cfg.market_data_root))
        lag = _effective_data_lag(health)
        if lag is None and _repo_empty():
            # 全新仓库:没有任何 ready 数据集,数据完全缺失。lag 无法
            # 计算,但"无数据"即"滞后无穷"——返回大数让 eod_sync_decide
            # 正常触发首次全量(否则自动同步永远不会开始,服务器首次
            # 部署后只能手动同步)。
            return 9999
        return lag

    def _repo_empty() -> bool:
        """True when the data root holds no ready dataset at all.

        A fresh server deployment has an empty (or missing) warehouse: lag
        cannot be computed, but there is nothing to be "fresh" about — the
        first sync must run in full-history mode regardless of the clock.
        """
        try:
            from .data.dataset_store import DatasetStore

            store = DatasetStore(cfg.market_data_root)
            for mid in store.list_manifests():
                m = store.load_manifest(mid, deep_copy=False)
                if m is not None and m.status == "ready":
                    return False
            return True
        except Exception:
            return False

    def _sync_in_progress() -> bool:
        try:
            with ctx.sync_lock:
                return bool(ctx.sync_state.get("running"))
        except Exception:
            return False

    # Restore the persisted trigger record across restarts: "today already
    # fired" must survive a service restart (otherwise a restart could fire a
    # second full chain), while a failed run stays eligible for same-day retry
    # via the retry_due logic in _check.
    _st0 = _load_state()
    if _st0.get("last_trigger_date") == _dt.date.today().strftime("%Y-%m-%d"):
        last_trigger_day = _dt.date.today()
    else:
        # a new day: drop stale retry bookkeeping from yesterday
        if _st0.get("retry_count") or _st0.get("pending_retry_at"):
            _save_state({"retry_count": 0, "pending_retry_at": None})

    def _trigger(reason: str, *, retry_count: Optional[int] = None) -> None:
        nonlocal last_trigger_day
        # retry_count is the numbered retry currently being launched. None
        # means the initial scheduled run and resets the counter to zero.
        attempt_retry_count = int(retry_count or 0)
        script = str(Path(__file__).resolve().parents[3] / "scripts" / "sync_market_data.py")
        today = int(_dt.date.today().strftime("%Y%m%d"))
        # overlay_v1 仓库:例行 EOD 走 delta 链(raw+factor 增量写 DuckDB,
        # 原子发布 watermark),不再每日重写完整行情 NPZ。回滚方式:关掉
        # ASTOCK_MARKET_STORAGE_MODE 后恢复旧 --mode incremental。
        overlay_mode = bool(
            (_os.environ.get("ASTOCK_MARKET_STORAGE_MODE", "").strip().lower())
            == "overlay_v1"
        )
        cmd = [
            sys.executable, "-u", script,
            "--source", "tushare", "--mode", "incremental",
            "--end-date", str(today), "--fresh",
            "--storage-root", str(cfg.market_data_root),
        ]
        if overlay_mode:
            cmd += ["--write-mode", "delta"]
        # 北交所纳入例行票池（默认开）：delta 链遇新票（首批北交所/新上市）
        # 会逐票拉全历史播种；关闭后退回沪深-only（旧行为）。
        if _env_flag("ASTOCK_EOD_SYNC_INCLUDE_BSE", "1"):
            cmd += ["--include-bse"]
        token = _os.environ.get("TUSHARE_TOKEN", "").strip()
        if token:
            cmd += ["--token", token]
        # 指数/ETF 增量链:股票链成功后顺序执行(同一次自动同步内),避免
        # 并发争抢 Tushare 频率限制。指数/ETF 无复权,与股票链互不影响。
        # overlay_v1 下指数/ETF 仍走旧 blob 增量路径(体积远小于股票全历史)。
        cmd_ie = None
        # 默认启用（与模块头部 env 说明一致）：ETF 权威面指针依赖本增量链
        # 刷新，关闭会导致新上市/退市 ETF 无法自动进出导出池
        if _env_flag("ASTOCK_EOD_SYNC_INDEX_ETF", "1"):
            cmd_ie = [
                sys.executable, "-u", script,
                "--source", "tushare", "--asset-class", "all",
                "--mode", "incremental",
                "--end-date", str(today), "--fresh",
                "--storage-root", str(cfg.market_data_root),
            ]
            if token:
                cmd_ie += ["--token", token]
        env = dict(_os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["MARKET_DATA_ROOT"] = str(cfg.market_data_root)

        # Child output goes to the data-root sync_logs instead of DEVNULL:
        # the run takes an hour+ and must leave a diagnosable trace (this was
        # the "EOD sync never seems to do anything" root cause).
        log_path = None
        log_fh = None
        try:
            log_dir = cfg.market_data_root / "sync_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"eod_sync_{today}.log"
            log_fh = open(log_path, "a", encoding="utf-8")
        except Exception as e:
            print(f"[EOD_SYNC] 无法打开日志文件: {e}，回退到 DEVNULL")
        print(
            f"[EOD_SYNC] {reason}，自动启动 Tushare 增量同步… "
            f"（日志: {log_path or 'DEVNULL'}）"
        )

        def _watch(proc: subprocess.Popen) -> None:
            """Wait the child and persist its outcome (exit code, retry due).

            Runs in a daemon thread so a failure is recorded even if the
            scheduler thread is busy; wakes the scheduler via wake_event.
            """
            rc = proc.wait()
            # 股票链退出码单独留存：后续 rc 会被指数/ETF 链覆盖，而指标复核
            # 只依赖股票数据面（IE 链 warning 级失败不应阻塞复核）。
            stock_rc = rc
            # 指数/ETF 链 warning 级失败（exit=2，如 ETF 面指针发布失败）
            # 不阻塞治理链：治理（consolidation/retention/GC）只作用于股票
            # overlay 层，与 IE 链数据无关。rc 保持非零以如实记录 partial
            # （数据 lag 门控不含 IE 面，同晚重试通常被拦下，指针由下一
            # 交易日的例行 IE 增量链幂等重写修复）。
            ie_soft_fail = False
            if rc == 0 and cmd_ie:
                print("[EOD_SYNC] 股票同步完成，启动指数/ETF 增量同步…")
                log_fh2 = None
                try:
                    log_fh2 = open(log_path, "a", encoding="utf-8")
                except Exception:
                    log_fh2 = None
                try:
                    proc2 = subprocess.Popen(
                        cmd_ie,
                        stdout=log_fh2 or subprocess.DEVNULL,
                        stderr=subprocess.STDOUT if log_fh2 else subprocess.DEVNULL,
                        env=env,
                    )
                    rc2 = proc2.wait()
                except Exception as e:
                    print(f"[EOD_SYNC] 指数/ETF 同步启动失败: {e}")
                    rc2 = -1
                finally:
                    if log_fh2:
                        log_fh2.close()
                if rc2 != 0:
                    print(f"[EOD_SYNC] 指数/ETF 同步失败（exit={rc2}）")
                    rc = rc2
                    if rc2 == 2:
                        ie_soft_fail = True
                        print(
                            "[EOD_SYNC] 指数/ETF 链为 warning 级 partial，"
                            "不阻塞治理链"
                        )
                else:
                    print("[EOD_SYNC] 指数/ETF 同步完成")
            governance_rc = None
            if (
                (rc == 0 or ie_soft_fail)
                and overlay_mode
                and _env_flag("ASTOCK_MARKET_GOVERNANCE_ENABLED", "1")
            ):
                govern_script = str(
                    Path(__file__).resolve().parents[3]
                    / "scripts"
                    / "govern_market_data.py"
                )
                govern_cmd = [
                    sys.executable,
                    "-u",
                    govern_script,
                    "--storage-root",
                    str(cfg.market_data_root),
                    "--maintain",
                    "--apply",
                ]
                govern_log = None
                try:
                    govern_log = open(log_path, "a", encoding="utf-8")
                except Exception:
                    govern_log = None
                try:
                    governance_proc = subprocess.Popen(
                        govern_cmd,
                        stdout=govern_log or subprocess.DEVNULL,
                        stderr=(
                            subprocess.STDOUT
                            if govern_log
                            else subprocess.DEVNULL
                        ),
                        env=env,
                    )
                    governance_rc = governance_proc.wait()
                except Exception as exc:
                    governance_rc = -1
                    print(f"[EOD_SYNC] 数据治理启动失败: {exc}")
                finally:
                    if govern_log:
                        govern_log.close()
                if governance_rc == 0:
                    print("[EOD_SYNC] consolidation/retention/GC 治理完成")
                else:
                    print(
                        f"[EOD_SYNC] 数据同步成功，但治理失败"
                        f"（exit={governance_rc}）"
                    )

            # 周五链指标复核（735/5日外）：股票同步成功即触发，治理失败不阻塞
            # （复核只依赖股票数据面）。结果写
            # storage/astock/indicator_review/review_{asof}.json，全市场导出
            # 读取后追加「735」「5日外」两个 sheet。失败不自动重试：下周五
            # 重来，或手动 `python -m wtpy.apps.astock review-weekly --asof <日>` 补跑。
            # --rules all：全规则预筛快照（阶段 1 契约）同链产出——下一段
            # 的 track 结算与网页筛选 cache-first 都依赖该发布快照。
            review_rc = None
            review_finished_at = None
            if stock_rc == 0:
                review_cmd = [
                    sys.executable, "-u", "-m", "wtpy.apps.astock",
                    # 显式锚定 storage_root：复核结果必须落在导出侧读取的同一
                    # storage（不依赖子进程 cwd/env 推导）
                    "--storage", str(cfg.storage_root),
                    "review-weekly", "--asof", str(today), "--rules", "all",
                ]
                review_log = None
                try:
                    review_log = open(
                        cfg.market_data_root / "sync_logs"
                        / f"indicator_review_{today}.log",
                        "a",
                        encoding="utf-8",
                    )
                except Exception:
                    review_log = None
                try:
                    print("[EOD_SYNC] 启动全市场指标复核（735/5日外）…")
                    review_proc = subprocess.Popen(
                        review_cmd,
                        stdout=review_log or subprocess.DEVNULL,
                        stderr=(
                            subprocess.STDOUT if review_log else subprocess.DEVNULL
                        ),
                        env=env,
                        # -m 导入 wtpy 包：显式锚定仓库根，不依赖继承的 cwd
                        cwd=str(Path(__file__).resolve().parents[3]),
                    )
                    review_rc = review_proc.wait()
                except Exception as e:
                    review_rc = -1
                    print(f"[EOD_SYNC] 指标复核启动失败: {e}")
                finally:
                    if review_log:
                        review_log.close()
                review_finished_at = _dt.datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if review_rc == 0:
                    print("[EOD_SYNC] 指标复核完成")
                else:
                    print(f"[EOD_SYNC] 指标复核失败（exit={review_rc}）")

            # 入选跟踪链（阶段 2）：串行结算**上一信号周**名单的次周收益
            # （本周新名单「待下周结算」，契约 §2）。--previous-week 使 CLI
            # 锚定最近发布快照周的上一信号周——审查 B-2：无该参数时锚
            # latest=今晚刚发布的新周，窗口未结束 → 恒 pending(3)。
            # gating（审查 B-4）：只看 stock_rc——track 的输入依赖是上周
            # 发布快照 + 本周行情，review 失败不影响这两个输入（latest
            # 指针在 review 失败时仍是上周的，完全确定）；no_snapshot 分
            # 支自己会兜住冷启动。各段独立：track 失败不影响 review 产物。
            tracking_rc = None
            tracking_finished_at = None
            tracking_week = None
            if stock_rc == 0:
                track_cmd = [
                    sys.executable, "-u", "-m", "wtpy.apps.astock",
                    "--storage", str(cfg.storage_root),
                    "track-weekly", "--previous-week",
                ]
                track_log = None
                try:
                    track_log = open(
                        cfg.market_data_root / "sync_logs"
                        / f"screen_tracking_{today}.log",
                        "a",
                        encoding="utf-8",
                    )
                except Exception:
                    track_log = None
                try:
                    print("[EOD_SYNC] 启动入选股票次周跟踪结算…")
                    track_proc = subprocess.Popen(
                        track_cmd,
                        stdout=track_log or subprocess.DEVNULL,
                        stderr=(
                            subprocess.STDOUT if track_log else subprocess.DEVNULL
                        ),
                        env=env,
                        # 与 review 段一致：-m 导入需锚定仓库根，不依赖 cwd
                        cwd=str(Path(__file__).resolve().parents[3]),
                    )
                    tracking_rc = track_proc.wait()
                except Exception as e:
                    tracking_rc = -1
                    print(f"[EOD_SYNC] 跟踪结算启动失败: {e}")
                finally:
                    if track_log:
                        track_log.close()
                tracking_finished_at = _dt.datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                # 记实际结算周（审查 B-1）：--previous-week 锚的是上一信号周，
                # 不是链触发日——从最新发布快照推导，读不到（冷启动无快照）
                # 时如实置 None，绝不拿 today 冒充
                tracking_week = None
                if tracking_rc is not None:
                    try:
                        from .service.screen_snapshots import (
                            latest_published_snapshot as _latest_snap,
                        )
                        from .service.screen_tracking import (
                            _load_calendar_or_none as _load_cal,
                            previous_signal_week as _prev_week,
                        )

                        _snap = _latest_snap(cfg)
                        _cal = _load_cal(cfg)
                        if _snap is not None and _cal is not None:
                            _prev = _prev_week(
                                _cal, int(_snap.get("week_id") or 0)
                            )
                            tracking_week = str(_prev) if _prev else None
                    except Exception:  # noqa: BLE001
                        tracking_week = None
                if tracking_rc == 0:
                    print(
                        f"[EOD_SYNC] 跟踪结算完成"
                        f"（信号周={tracking_week or '未知'}）"
                    )
                else:
                    # 独立 exit code 只记账：不影响 review 产物，也不让 rc
                    # 反映 track 的失败（治理/复核同理——各自独立可观测）
                    print(
                        f"[EOD_SYNC] 跟踪结算未完成（exit={tracking_rc}，"
                        f"信号周={tracking_week or '未知'}，产物不受影响；"
                        f"可手动 track-weekly --previous-week 补）"
                    )

            # 链尾自动生成全市场数据表（2026-09-23 需求）：大盘指数/ETF/所有A股，
            # 不含指标筛选 sheet；产物与状态文件落 storage/astock/，前端直接下载。
            # gating 只看 stock_rc（行情面成功即可；review/track 失败不影响导出
            # 的输入依赖，且变卦/高岛列只依赖卦象库与行情，不依赖跟踪产物）。
            auto_export_rc = None
            auto_export_finished_at = None
            if stock_rc == 0 and _env_flag("ASTOCK_EOD_AUTO_EXPORT_ENABLED", "1"):
                export_cmd = [
                    sys.executable, "-u", "-m", "wtpy.apps.astock",
                    "--storage", str(cfg.storage_root),
                    "export-weekly", "--date", str(today),
                ]
                export_log = None
                try:
                    export_log = open(
                        cfg.market_data_root / "sync_logs"
                        / f"auto_export_{today}.log",
                        "a",
                        encoding="utf-8",
                    )
                except Exception:
                    export_log = None
                try:
                    print("[EOD_SYNC] 启动全市场数据表自动生成（可下载）…")
                    export_proc = subprocess.Popen(
                        export_cmd,
                        stdout=export_log or subprocess.DEVNULL,
                        stderr=(
                            subprocess.STDOUT if export_log else subprocess.DEVNULL
                        ),
                        env=env,
                        cwd=str(Path(__file__).resolve().parents[3]),
                    )
                    auto_export_rc = export_proc.wait()
                except Exception as e:
                    auto_export_rc = -1
                    print(f"[EOD_SYNC] 自动导出启动失败: {e}")
                finally:
                    if export_log:
                        export_log.close()
                auto_export_finished_at = _dt.datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if auto_export_rc == 0:
                    print("[EOD_SYNC] 全市场数据表已生成")
                else:
                    # 附属产物失败不影响本次同步的成功记账（重试语义只在
                    # 行情主链上）；可手动 export-weekly 补跑。
                    print(
                        f"[EOD_SYNC] 全市场数据表生成失败（exit={auto_export_rc}，"
                        f"产物留旧；可手动 export-weekly 补）"
                    )

            st = _load_state()
            prev_retry = int(st.get("retry_count") or 0)
            finished = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            extra = {
                "last_sync_exit_code": rc,
                "last_sync_finished_at": finished,
                "sync_pid": int(proc.pid or 0),
                "last_governance_exit_code": governance_rc,
                "last_governance_finished_at": (
                    finished if governance_rc is not None else None
                ),
                "last_indicator_review_exit_code": review_rc,
                "last_indicator_review_finished_at": review_finished_at,
                "last_indicator_review_asof": (
                    str(today) if review_rc is not None else None
                ),
                # 跟踪链独立退出码记账（不影响 last_sync_exit_code 的重试语义）
                "last_tracking_exit_code": tracking_rc,
                "last_tracking_finished_at": tracking_finished_at,
                "last_tracking_week": tracking_week,
                # 链尾自动导出记账（附属产物：失败不改变 rc 的重试语义）
                "last_auto_export_exit_code": auto_export_rc,
                "last_auto_export_finished_at": auto_export_finished_at,
            }
            if rc == 0:
                extra["retry_count"] = 0
                extra["pending_retry_at"] = None
                print("[EOD_SYNC] 同步成功（exit=0）")
            else:
                retry = prev_retry + 1
                extra["retry_count"] = retry
                extra["pending_retry_at"] = (
                    (_dt.datetime.now() + _dt.timedelta(seconds=poll_sec)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    if retry <= max_retries
                    else None
                )
                if retry <= max_retries:
                    print(
                        f"[EOD_SYNC] 同步失败（exit={rc}），"
                        f"{poll_sec // 60} 分钟后重试（第 {retry}/{max_retries} 次）"
                    )
                else:
                    print(f"[EOD_SYNC] 同步失败（exit={rc}），今日重试次数已用尽")
            _save_state(extra)
            wake_event.set()

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh or subprocess.DEVNULL,
                stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
                env=env,
            )
        except Exception as e:
            if log_fh:
                log_fh.close()
            print(f"[EOD_SYNC] 启动失败: {e}")
            # Spawn failure is treated like a child failure: persist a non-zero
            # exit record and wake the scheduler so the same retry loop (30 min
            # later) takes over — otherwise the day would silently look "done".
            _save_state({
                "last_trigger_date": _dt.date.today().strftime("%Y-%m-%d"),
                "last_sync_started_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "last_reason": f"{reason}（启动失败）",
                "last_sync_exit_code": -1,
                "last_sync_finished_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "retry_count": attempt_retry_count + 1,
                "pending_retry_at": (
                    (_dt.datetime.now() + _dt.timedelta(seconds=poll_sec)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    if attempt_retry_count + 1 <= max_retries
                    else None
                ),
            })
            wake_event.set()
            return
        if log_fh:
            # the child keeps the handle; the parent must close its copy
            log_fh.close()
        print(f"[EOD_SYNC] 已启动后台进程 PID={proc.pid}")
        last_trigger_day = _dt.date.today()
        _save_state({
            "last_trigger_date": last_trigger_day.strftime("%Y-%m-%d"),
            "last_sync_started_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_reason": reason,
            "sync_pid": int(proc.pid or 0),
            "retry_count": attempt_retry_count,
            "pending_retry_at": None,
        })
        _thr.Thread(
            target=_watch, args=(proc,), daemon=True, name="astock-eod-sync-watch"
        ).start()

    def _retry_due(st: dict, now: _dt.datetime) -> bool:
        """True when no pending retry timestamp exists or it has already passed.

        Keeps a service restart from retrying immediately (it would otherwise
        bypass the poll_seconds retry interval set by the watcher thread).
        """
        pend = st.get("pending_retry_at")
        if not pend:
            return True
        try:
            return now >= _dt.datetime.strptime(pend, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return True

    def _forward_days() -> Optional[List[int]]:
        """前瞻交易日历（开市日升序）；last_trading_day 模式下惰性刷新。

        与本周判定无关的日子不联网：仅当缓存缺失/过期/覆盖不到下周时，
        由 refresh_forward_calendar 联网拉取（限速重试在 provider 层）。
        任何失败返回已有缓存或 None——调用方按 None 退化到固定周几。
        """
        if schedule_mode != _sch.SCHEDULE_LAST_TRADING_DAY:
            return None
        try:
            token = _os.environ.get("TUSHARE_TOKEN", "").strip() or None
            # storage_root 即 cfg.storage_root（storage/astock），与状态文件
            # 同目录；测试用 SimpleNamespace(cfg) 也具备该属性。
            _sch.refresh_forward_calendar(Path(cfg.storage_root), token=token)
        except Exception as e:  # noqa: BLE001 — 日历不可用时日历即 None
            print(f"[EOD_SYNC] 前瞻交易日历刷新跳过: {type(e).__name__}: {e}")
        return _sch.load_forward_open_dates(Path(cfg.storage_root))

    def _check(label: str) -> bool:
        """Returns True when a sync was triggered."""
        now = _dt.datetime.now()
        # Same-day retry: today already fired but the run failed and the
        # retry budget is not exhausted -> allow eod_sync_decide to fire
        # again (it would otherwise short-circuit on last_trigger_day).
        effective_last = last_trigger_day
        st = _load_state()
        last_rc = st.get("last_sync_exit_code")
        retry_number: Optional[int] = None
        if (
            effective_last is not None
            and effective_last == now.date()
            and (last_rc or 0) != 0
            and 0 < int(st.get("retry_count") or 0) <= max_retries
            and _retry_due(st, now)
        ):
            effective_last = None
            retry_number = int(st.get("retry_count") or 0)
            print(
                f"[EOD_SYNC] previous run failed (exit={last_rc}); "
                f"retry {retry_number}/{max_retries}"
            )
        trading_days = _forward_days()
        # 低成本日期/时刻门先行：不占健康检查（全链路 lag 计算）给非候选日。
        # 同日重试只在触发日当天发生，门恒通过，无需特判。
        eligible, day_reason, _mode_eff = eod_sync_day_gate(
            now=now,
            sync_time=sync_time,
            sync_weekday=sync_weekday,
            schedule_mode=schedule_mode,
            trading_days=trading_days,
        )
        if not eligible:
            print(f"[EOD_SYNC] {label}：{day_reason}")
            return False
        trigger, reason, today_key = eod_sync_decide(
            lag=_lag_days(),
            now=now,
            sync_time=sync_time,
            sync_weekday=sync_weekday,
            min_lag=min_lag,
            last_trigger_day=effective_last,
            schedule_mode=schedule_mode,
            trading_days=trading_days,
        )
        if trigger:
            if _sync_in_progress():
                print(f"[EOD_SYNC] {label}：{reason}，但已有手动同步在运行，稍后重试")
                return False
            _trigger(
                f"{label}: {reason}", retry_count=retry_number
            )
            return True
        print(f"[EOD_SYNC] {label}：{reason}")
        return False

    def _sleep_until(hhmm: str) -> None:
        """Sleep until today's hh:mm (or tomorrow if already passed)."""
        now = _dt.datetime.now()
        try:
            target = now.replace(
                hour=int(hhmm[:2]), minute=int(hhmm[3:5]), second=0, microsecond=0
            )
        except (ValueError, IndexError):
            target = now.replace(hour=18, minute=30, second=0, microsecond=0)
        if target <= now:
            target += _dt.timedelta(days=1)
        _time.sleep(max(1.0, (target - now).total_seconds()))

    def _candidate_weekdays() -> frozenset:
        """调度线程需要醒来的工作日集合。

        last_trading_day 模式：周一~周五都可能是本周最后交易日（节假日前移），
        每天 sync_time 醒一次做日历判定；weekday 模式只醒 sync_weekday。
        """
        if schedule_mode == _sch.SCHEDULE_LAST_TRADING_DAY:
            return frozenset({0, 1, 2, 3, 4})
        return frozenset({sync_weekday})

    def _next_sync_target(now) -> _dt.datetime:
        """Next candidate check time, strictly after ``now``.

        last_trading_day 模式 = 下一个工作日（周一~周五）的 sync_time；
        weekday 模式 = 下一个 sync_weekday 的 sync_time。
        """
        try:
            hour, minute = int(sync_time[:2]), int(sync_time[3:5])
        except (ValueError, IndexError):
            hour, minute = 18, 30
        days = (sync_weekday - now.weekday()) % 7
        target = (now + _dt.timedelta(days=days)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if target <= now:
            target += _dt.timedelta(days=7)
        if schedule_mode == _sch.SCHEDULE_LAST_TRADING_DAY:
            target = _sch.next_candidate_at(
                now, sync_time, weekdays=set(_candidate_weekdays())
            )
        return target

    def _today_trigger_record() -> dict:
        st = _load_state()
        today_key = _dt.date.today().strftime("%Y-%m-%d")
        if st.get("last_trigger_date") != today_key:
            return {}
        return st

    if startup_check:
        try:
            _check("启动检查")
        except Exception as e:
            print(f"[EOD_SYNC] 启动检查异常: {type(e).__name__}: {e}")
    _save_state()

    while True:
        try:
            now = _dt.datetime.now()
            if now.weekday() not in _candidate_weekdays():
                target = _next_sync_target(now)
                wake_event.wait(
                    timeout=max(1.0, (target - now).total_seconds())
                )
                continue
            if now.strftime("%H:%M") < sync_time:
                _sleep_until(sync_time)
                continue
            st = _today_trigger_record()
            if not st:
                # 非触发日快速通道（last_trading_day 模式）：今天不是本周最后
                # 交易日时直接睡到下一个候选日，不进入 30 分钟轮询（那是留给
                # 触发日当天「Tushare 数据迟到」的重试节奏）。
                if schedule_mode == _sch.SCHEDULE_LAST_TRADING_DAY:
                    day_ok, day_reason, _mode_eff = eod_sync_day_gate(
                        now=now,
                        sync_time=sync_time,
                        sync_weekday=sync_weekday,
                        schedule_mode=schedule_mode,
                        trading_days=_forward_days(),
                    )
                    if not day_ok:
                        print(f"[EOD_SYNC] 收盘后定时检查：{day_reason}")
                        wake_event.clear()
                        wake_event.wait(
                            timeout=max(
                                1.0, (_next_sync_target(now) - now).total_seconds()
                            )
                        )
                        continue
                # nothing fired today: normal scheduled check
                fired = _check("收盘后定时检查")
                wake_event.clear()
                if fired:
                    # wait for the watcher to report the outcome, or until
                    # tomorrow's sync time if it never does
                    wake_event.wait(timeout=max(1.0, (_next_sync_target(now) - now).total_seconds()))
                else:
                    wake_event.wait(timeout=poll_sec)
                continue
            if int(st.get("last_sync_exit_code") or 0) == 0:
                # This week's run succeeded; wait until next Friday.
                target = _next_sync_target(now)
                wake_event.wait(
                    timeout=max(1.0, (target - now).total_seconds())
                )
                continue
            if int(st.get("retry_count") or 0) > max_retries:
                # Retry budget used up; wait until next scheduled Friday.
                target = _next_sync_target(now)
                wake_event.wait(
                    timeout=max(1.0, (target - now).total_seconds())
                )
                continue
            # a run failed and a retry is pending: wait until its due time
            pending = st.get("pending_retry_at")
            pend_dt = None
            if pending:
                try:
                    pend_dt = _dt.datetime.strptime(pending, "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    pend_dt = None
            if pend_dt and now < pend_dt:
                wake_event.clear()
                wake_event.wait(
                    timeout=min(poll_sec, max(10.0, (pend_dt - now).total_seconds()))
                )
                continue
            # retry due: _check re-arms the run (data-lag gate still applies)
            fired = _check("失败重试")
            wake_event.clear()
            if fired:
                wake_event.wait(timeout=max(1.0, (_next_sync_target(now) - now).total_seconds()))
            else:
                # data caught up or a manual sync took over: stop retrying
                _save_state({"retry_count": max_retries, "pending_retry_at": None})
                _sleep_until(sync_time)
        except Exception as e:
            print(f"[EOD_SYNC] 定时检查异常: {type(e).__name__}: {e}")
            _time.sleep(300)


def create_app(cfg: Optional[AStockConfig] = None) -> FastAPI:
    cfg = cfg or get_default_config()
    cfg.ensure_dirs()
    app = FastAPI(
        title="AStock Backtest Console",
        version=get_version_string(),
    )

    @app.middleware("http")
    async def _reject_oversized_body(request, call_next):
        if request.method in _LARGE_BODY_METHODS:
            path = request.url.path
            raw = request.headers.get("content-length")
            if raw is None:
                # No declared length: chunked bodies could bypass the small
                # rules cap, so require Content-Length there. A request with
                # neither header explicitly has no body and passes through.
                if path.startswith("/api/v1/rules") and request.headers.get(
                    "transfer-encoding"
                ):
                    return JSONResponse(
                        {"detail": "Content-Length required for this endpoint"},
                        status_code=411,
                    )
            else:
                try:
                    length = int(raw)
                except ValueError:
                    length = None
                if length is not None:
                    limit = _body_size_limit(path)
                    if length > limit:
                        return JSONResponse(
                            {
                                "detail": "request body too large "
                                f"(max {limit // (1024 * 1024)}MB)"
                            },
                            status_code=413,
                        )
        return await call_next(request)

    app.state.astock = ApiContext(
        cfg=cfg,
        rules=RuleService(cfg),
        jobs=JobStore(
            cfg,
            max_workers=resolve_bt_max_workers(
                None, persisted=get_app_setting(cfg, "bt_max_workers")
            ),
        ),
        bt_svc=BacktestService(cfg),
        forecast=ForecastService(cfg),
    )
    for router in _ALL_ROUTERS:
        app.include_router(router)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


def serve(host: str = "127.0.0.1", port: int = 8765, cfg: Optional[AStockConfig] = None) -> None:
    import uvicorn

    from .config import load_env_file, market_data_root_guard

    # Machine-local settings (MARKET_DATA_ROOT, ASTOCK_ENV, ...) live in .env
    # at the project root; existing environment variables always win.
    load_env_file()

    cfg = cfg or get_default_config()
    guard = market_data_root_guard(cfg)

    ready_count = None
    product_line = "—"
    try:
        from .data.dataset_store import DatasetStore
        from .data.repository import MarketDataRepository
        from .data.tushare_product import resolve_active_tushare_product_pair

        if cfg.market_data_root.exists():
            store = DatasetStore(cfg.market_data_root)
            repo = MarketDataRepository(store)
            all_ds = repo.list_datasets()
            ready_count = sum(1 for d in all_ds if d.status == "ready")
            pair = resolve_active_tushare_product_pair(store)
            if pair is not None:
                product_line = (
                    f"L1={pair.l1_dataset_id} | L2={pair.l2_dataset_id} "
                    f"| cutoff={pair.cutoff}"
                )
            else:
                product_line = "formal L1/L2 not ready (Tushare-only)"
        else:
            ready_count = 0
    except Exception as e:  # pragma: no cover - startup banner must never crash
        ready_count = f"unavailable ({e})"

    print("=" * 64)
    print("AStock Console startup")
    print(f"  ASTOCK_ENV        : {guard['astock_env']}")
    print(f"  MARKET_DATA_ROOT  : {guard['market_data_root']}"
          + ("  [INTERNAL TEST ROOT]" if guard["is_internal"] else "  [external]"))
    print(f"  env var set       : {guard['market_data_root_env_set']}")
    print(f"  ready datasets    : {ready_count}")
    print(f"  Tushare product   : {product_line}")
    print("=" * 64)

    if isinstance(ready_count, int) and ready_count == 0 and cfg.market_data_root.exists():
        # Data root has no usable datasets: freshness cannot be judged, so the
        # EOD auto-sync will skip. Fail loud instead of silently doing nothing.
        print("!! 数据根内没有可用数据集（manifests 缺失或全部未 ready）")
        print("!! 18:30 自动更新会跳过（无法计算数据滞后）。请先手动执行一次同步，或")
        print(f"!! 运行体检确认数据根格式: python scripts/check_data_root.py"
              f" --storage-root {cfg.market_data_root}")
        print("=" * 64)

    if guard["blocked"]:
        print("!! STARTUP BLOCKED (production data-root guard)")
        print(f"!! reason  : {guard['reason']}")
        print("!! fix     : set MARKET_DATA_ROOT to the production data root in .env")
        print(f"!! override: {guard['override_allowed_by']} (NOT recommended)")
        raise SystemExit(2)
    if guard["is_internal"] and guard["astock_env"] != "production":
        print("WARNING: using INTERNAL project test data root "
              "(set MARKET_DATA_ROOT / ASTOCK_ENV=production for formal use)")

    app = create_app(cfg)

    # Check CA freshness on startup, but only write in the weekly Friday window.
    def _auto_ca_check():
        """CA 事件自动同步：启动检查（>30 天）+ 每周五定时增量同步。

        Startup only reports staleness outside the weekly window. The
        actual sync runs on Friday at ``ASTOCK_CA_SYNC_TIME`` (23:00 by
        default), after the market-data chain. Child output goes to
        sync_logs/ca_sync_<date>.log and the exit code is persisted to
        eod_sync_state.json.
        """
        import datetime as _dt
        import os as _os
        import threading as _thr
        import time as _time

        state_path_env = _os.environ.get("ASTOCK_EOD_STATE_PATH")
        state_path = Path(state_path_env) if state_path_env else (
            Path(__file__).resolve().parents[3] / "storage" / "astock" / "eod_sync_state.json"
        )
        ca_time = _os.environ.get("ASTOCK_CA_SYNC_TIME", "23:00")
        try:
            ca_weekday = min(
                6, max(0, int(_os.environ.get("ASTOCK_CA_SYNC_WEEKDAY", "4")))
            )
        except (TypeError, ValueError):
            ca_weekday = 4

        def _load_state():
            try:
                if state_path.exists():
                    return json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
            return {}

        def _save_state(extra):
            try:
                with _EOD_STATE_LOCK:
                    st = _load_state()
                    st.update(extra)
                    st["updated_at"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    _tmp = state_path.with_suffix(".json.tmp")
                    _tmp.write_text(
                        json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    _tmp.replace(state_path)  # atomic: readers never see a torn file
            except Exception:
                pass

        def _spawn_ca(reason: str) -> None:
            script = str(Path(__file__).resolve().parents[3] / "scripts" / "sync_ca_events.py")
            today = int(_dt.date.today().strftime("%Y%m%d"))
            cmd = [sys.executable, "-u", script, "--mode", "incremental", "--days", "90",
                   "--storage-root", str(cfg.market_data_root)]
            env = dict(_os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["MARKET_DATA_ROOT"] = str(cfg.market_data_root)
            log_path = None
            log_fh = None
            try:
                log_dir = cfg.market_data_root / "sync_logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                log_path = log_dir / f"ca_sync_{today}.log"
                log_fh = open(log_path, "a", encoding="utf-8")
            except Exception as e:
                print(f"[CA_AUTO] 无法打开日志文件: {e}，回退到 DEVNULL")

            def _watch(proc):
                rc = proc.wait()
                _save_state({
                    "ca_sync_exit_code": rc,
                    "ca_sync_finished_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "ca_sync_pid": int(proc.pid or 0),
                })
                print(
                    f"[CA_AUTO] CA 同步{'成功' if rc == 0 else f'失败（exit={rc}），下周五自动重跑'}"
                )

            print(f"[CA_AUTO] {reason}，自动启动 CA 增量同步… （日志: {log_path or 'DEVNULL'}）")
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_fh or subprocess.DEVNULL,
                    stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
                    env=env,
                )
            except Exception as e:
                if log_fh:
                    log_fh.close()
                print(f"[CA_AUTO] 启动失败: {e}")
                return
            if log_fh:
                log_fh.close()
            print(f"[CA_AUTO] 已启动后台进程 PID={proc.pid}")
            _save_state({
                "ca_sync_started_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ca_sync_pid": int(proc.pid or 0),
            })
            _thr.Thread(target=_watch, args=(proc,), daemon=True,
                        name="astock-ca-sync-watch").start()

        # ---- startup check: never synced / stale > 30 days -> sync now ----
        ca_meta = cfg.market_data_root / "ca_events" / "_meta.json"
        need_sync = False
        reason = ""
        if not ca_meta.exists():
            need_sync = True
            reason = "CA数据从未同步"
        else:
            try:
                with open(ca_meta, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                last = meta.get("last_sync_at", "")
                if last:
                    last_dt = _dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                    days_ago = (_dt.datetime.now() - last_dt).days
                    if days_ago > 30:
                        need_sync = True
                        reason = f"CA数据已 {days_ago} 天未更新"
                else:
                    need_sync = True
                    reason = "CA元数据缺少同步时间"
            except Exception:
                need_sync = True
                reason = "CA元数据读取失败"
        now = _dt.datetime.now()
        if (
            need_sync
            and now.weekday() == ca_weekday
            and now.strftime("%H:%M") >= ca_time
        ):
            _spawn_ca(reason)
        elif need_sync:
            print("[CA_AUTO] CA数据待更新，将在周五计划窗口执行")
        else:
            print("[CA_AUTO] CA数据在有效期内，跳过启动同步")

        # ---- weekly timer: Friday after the market-data chain ----
        def _sleep_until(hhmm):
            now = _dt.datetime.now()
            try:
                target = now.replace(
                    hour=int(hhmm[:2]), minute=int(hhmm[3:5]), second=0, microsecond=0
                )
            except (ValueError, IndexError):
                target = now.replace(hour=18, minute=35, second=0, microsecond=0)
            if target <= now:
                target += _dt.timedelta(days=1)
            _time.sleep(max(1.0, (target - now).total_seconds()))

        while True:
            try:
                now = _dt.datetime.now()
                if now.weekday() != ca_weekday:
                    days = (ca_weekday - now.weekday()) % 7
                    try:
                        target = (now + _dt.timedelta(days=days)).replace(
                            hour=int(ca_time[:2]),
                            minute=int(ca_time[3:5]),
                            second=0,
                            microsecond=0,
                        )
                    except (ValueError, IndexError):
                        target = (now + _dt.timedelta(days=days)).replace(
                            hour=23, minute=0, second=0, microsecond=0
                        )
                    _time.sleep(max(1.0, (target - now).total_seconds()))
                    continue
                if now.strftime("%H:%M") < ca_time:
                    _sleep_until(ca_time)
                    continue
                # a startup-check run earlier today already covered it
                if _load_state().get("ca_sync_started_at", "")[:10] == now.strftime("%Y-%m-%d"):
                    _sleep_until(ca_time)
                    continue
                _spawn_ca(f"每周五定时（{ca_time}）")
                _sleep_until(ca_time)
            except Exception as e:
                print(f"[CA_AUTO] 定时检查异常: {type(e).__name__}: {e}")
                _time.sleep(300)

    import threading as _thr
    _thr.Thread(target=_auto_ca_check, daemon=True).start()

    def _auto_tushare_product_reconcile() -> None:
        """Build formal products from existing local Tushare parents only."""
        try:
            from .data.dataset_store import DatasetStore
            from .data.tushare_product import reconcile_tushare_product_datasets

            if not cfg.market_data_root.exists():
                print("[TUSHARE_PRODUCT] data root missing; waiting for sync")
                return
            if cfg.market_storage_overlay_enabled:
                # overlay_v1: 正式 L1/L2 是运行时虚拟视图(基准 blob + DuckDB
                # 增量),不再物化快照;跳过产品派生,避免例行更新重建完整行情 NPZ。
                print(
                    "[TUSHARE_PRODUCT] overlay_v1 active: virtual L1/L2 views "
                    "reflect the delta automatically; skipping materialized "
                    "reconcile"
                )
                return
            result = reconcile_tushare_product_datasets(
                DatasetStore(cfg.market_data_root)
            )
            print(
                "[TUSHARE_PRODUCT] "
                f"status={result.status} "
                f"L1={result.l1_dataset_id or '-'} "
                f"L2={result.l2_dataset_id or '-'} "
                f"missing={result.missing or '-'} "
                f"issues={result.issues or '-'}"
            )
        except Exception as exc:
            # Product migration must never prevent the API from starting.
            print(
                "[TUSHARE_PRODUCT] reconcile failed: "
                f"{type(exc).__name__}: {exc}"
            )

    _thr.Thread(
        target=_auto_tushare_product_reconcile,
        daemon=True,
        name="astock-tushare-product-reconcile",
    ).start()

    # Automatic EOD market-data sync: startup freshness check + scheduled
    # weekday sync after market close (env-configurable, see _auto_eod_sync).
    _thr.Thread(
        target=_auto_eod_sync,
        args=(cfg, app.state.astock),
        daemon=True,
        name="astock-eod-sync",
    ).start()

    # heavy-job 待办重试（契约 §7）：抢锁失败的任务不能等到下周五——服务
    # 运行期按 5/15/30 分钟有界退避轮询补跑，不依赖重启。
    _thr.Thread(
        target=_auto_heavy_job_retry,
        args=(cfg, app.state.astock),
        daemon=True,
        name="astock-heavy-job-retry",
    ).start()

    uvicorn.run(app, host=host, port=port)


def _heavy_job_command(task_key: str, storage_root: Path) -> Optional[List[str]]:
    """heavy-job 待办 → CLI 命令（契约 §7 补跑映射）。

    可测的纯函数（后台线程里做字符串解析必须锁住映射）：
      track_<YYYYMMDD>     → track-weekly --week <D>
      backfill_<N>         → track-weekly --backfill <N>
      review_all_<YYYYMMDD>→ review-weekly --rules all --asof <D>
      auto_export_<YYYYMMDD>→ export-weekly --date <D>
    未知类型 / 参数不合法 → None（调用方跳过并保留欠账，绝不猜命令）。
    格式校验防"另一个体系的 key 被解析成非法命令"（如 tracking_task_key
    产生的 track_snap_xxx 会被当 --week 传给 CLI → exit 2 误标 exhausted）。
    """
    cmd = [
        sys.executable, "-u", "-m", "wtpy.apps.astock",
        "--storage", str(storage_root),
    ]

    def _is_date(s: str) -> bool:
        return len(s) == 8 and s.isdigit()

    if task_key.startswith("auto_export_"):
        arg = task_key.split("_", 2)[2]
        if not _is_date(arg):
            return None
        return cmd + ["export-weekly", "--date", arg]
    if task_key.startswith("track_"):
        arg = task_key.split("_", 1)[1]
        if not _is_date(arg):
            return None
        return cmd + ["track-weekly", "--week", arg]
    if task_key.startswith("backfill_"):
        arg = task_key.split("_", 1)[1]
        if not (arg.isdigit() and int(arg) > 0):
            return None
        return cmd + ["track-weekly", "--backfill", arg]
    if task_key.startswith("review_all_"):
        arg = task_key.split("_", 2)[2]
        if arg == "0":
            # 手动 review-weekly --rules all 不带 --asof：按最新数据面重算
            # （0 是 asof 缺省的记号，不是日期——合法任务，不能当非法键）
            return cmd + ["review-weekly", "--rules", "all"]
        if not _is_date(arg):
            return None
        return cmd + ["review-weekly", "--rules", "all", "--asof", arg]
    return None


def _auto_heavy_job_retry(cfg: AStockConfig, ctx: "ApiContext") -> None:
    """heavy-job 待办的服务运行期重试循环（契约 §7 有界退避）。

    每 ``ASTOCK_HEAVY_JOB_RETRY_POLL_SECONDS``（默认 120s）检查一次持久化
    待办；到期的任务以**子进程**方式补跑（与周五链同构：重任务不占 API
    进程内存）。串行 + 每轮最多 1 个（9/13 OOM 教训：绝不并发重任务）。
    EOD 同步进行中时跳过本轮（让行情面先落地，避免读半截数据）。

    环境变量：
      ASTOCK_HEAVY_JOB_RETRY_ENABLED=0|1   (默认 1)
      ASTOCK_HEAVY_JOB_RETRY_POLL_SECONDS=N (默认 120, 最小 30)
    """
    from .service import heavy_job as _hj
    import os as _os
    import time as _time

    # _env_flag 是 _auto_eod_sync 内的局部函数：本线程按**完全相同**的
    # 语义独立实现（白名单 in ("1","true","yes","on")），避免两处判定漂移
    def _env_flag(name: str, default: str = "1") -> bool:
        return str(_os.environ.get(name, default)).strip().lower() in (
            "1", "true", "yes", "on",
        )

    if not _env_flag("ASTOCK_HEAVY_JOB_RETRY_ENABLED", "1"):
        print("[HEAVY_JOB] 待办重试已禁用（ASTOCK_HEAVY_JOB_RETRY_ENABLED=0）")
        return
    poll = max(30, int(_os.environ.get("ASTOCK_HEAVY_JOB_RETRY_POLL_SECONDS", "120")))
    storage_root = Path(cfg.storage_root)
    repo_root = Path(__file__).resolve().parents[3]
    # 启动延迟：不与启动期的对账/同步抢资源
    _time.sleep(min(60, poll))

    def _runner(task_key: str) -> bool:
        """按 task_key 解析并 spawn 对应 CLI（子进程持锁，父不持）。"""
        cmd = _heavy_job_command(task_key, storage_root)
        if cmd is None:
            # 无法映射的键（历史版本格式/手工写入）：标欠账并退出自动
            # 重试。否则它会一直到期 → 每轮 120s 空转一次，永不收敛。
            from .service import screen_contract as _sc_mod

            _sc_mod.record_pending_job(
                storage_root, task_key, reason="unmappable_task_key",
                mark_exhausted=True,
            )
            print(f"[HEAVY_JOB] 未知待办类型，标记欠账不再自动重试: {task_key}")
            return False
        log_path = cfg.market_data_root / "sync_logs" / f"heavy_job_{task_key}.log"
        log_fh = None
        try:
            log_fh = open(log_path, "a", encoding="utf-8")
        except Exception:  # noqa: BLE001
            log_fh = None
        try:
            print(f"[HEAVY_JOB] 补跑待办: {task_key}")
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh or subprocess.DEVNULL,
                stderr=(subprocess.STDOUT if log_fh else subprocess.DEVNULL),
                cwd=str(repo_root),
            )
            rc = proc.wait()
        except Exception as e:  # noqa: BLE001
            info = _hj.record_runner_spawn_failure(storage_root, task_key)
            print(
                f"[HEAVY_JOB] 补跑启动失败（已记账退避，"
                f"attempts={info.get('attempts')}）: {e}"
            )
            return False
        finally:
            if log_fh:
                log_fh.close()
        # 0=完成；3=可重试（子进程内已按真实 completion 记账：pending/
        # blocked_benchmark/data_version_changed/skipped_locked，抢锁失败
        # 在 run_with_heavy_lock 内已记一次）——本层对 0/3 不重复写；
        # 1/2=不可重试标欠账；其余异常码（信号杀死/OOM/崩溃）按有界退避记账。
        print(f"[HEAVY_JOB] {task_key} 退出码={rc}")
        info = _hj.record_runner_exit(storage_root, task_key, rc)
        if info is not None:
            print(
                f"[HEAVY_JOB] {task_key} {info.get('reason')}"
                f"（attempts={info.get('attempts')}，"
                f"exhausted={info.get('exhausted')}）"
            )
            return False
        return rc == 0

    while True:
        try:
            _time.sleep(poll)
            # EOD 同步进行中：让行情面先落地（避免读半截数据）
            with ctx.sync_lock:
                if ctx.sync_state.get("running"):
                    continue
            jobs = _hj.pending_retry_due(storage_root)
            if not jobs:
                continue
            _hj.retry_due_jobs(storage_root, runner=_runner, max_per_pass=1)
        except Exception as e:  # noqa: BLE001 — 轮询循环绝不因单次异常退出
            print(f"[HEAVY_JOB] 重试轮询异常（继续）: {e}")


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="astock-serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--tdx-root", default=None)
    p.add_argument("--storage", default=None)
    args = p.parse_args(argv)
    overrides: Dict[str, Any] = {}
    if args.tdx_root:
        overrides["tdx_root"] = Path(args.tdx_root)
    if args.storage:
        overrides["storage_root"] = Path(args.storage)
    cfg = get_default_config(**overrides) if overrides else get_default_config()
    serve(host=args.host, port=args.port, cfg=cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
