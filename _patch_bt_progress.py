# -*- coding: utf-8 -*-
from pathlib import Path

p = Path(r"E:\Software Development\wtpy-master\wtpy\apps\astock\service\backtest.py")
t = p.read_text(encoding="utf-8")

# Ensure typing Any imported
if "from typing import Any" not in t and "Any," not in t.split("from typing import", 1)[-1][:80]:
    t = t.replace(
        "from typing import Any, Dict, List, Optional, Sequence, Union",
        "from typing import Any, Callable, Dict, List, Optional, Sequence, Union",
        1,
    )
else:
    t = t.replace(
        "from typing import Any, Dict, List, Optional, Sequence, Union",
        "from typing import Any, Callable, Dict, List, Optional, Sequence, Union",
        1,
    )

replacements = []

replacements.append((
'''class BacktestService:
    def __init__(self, cfg: Optional[AStockConfig] = None):
        self.cfg = cfg or get_default_config()
        self.cfg.ensure_dirs()
        self.rules = RuleService(self.cfg)

    def run(self, req: BacktestRequest) -> Dict[str, Any]:
        return run_backtest(self.cfg, req, rules=self.rules)


def run_backtest(
    cfg: AStockConfig,
    req: BacktestRequest,
    *,
    rules: Optional[RuleService] = None,
) -> Dict[str, Any]:
    """Execute portfolio backtest; returns summary dict (also writes outputs)."""
''',
'''class BacktestService:
    def __init__(self, cfg: Optional[AStockConfig] = None):
        self.cfg = cfg or get_default_config()
        self.cfg.ensure_dirs()
        self.rules = RuleService(self.cfg)

    def run(
        self,
        req: BacktestRequest,
        *,
        progress_cb: Optional[Any] = None,
    ) -> Dict[str, Any]:
        return run_backtest(self.cfg, req, rules=self.rules, progress_cb=progress_cb)


def run_backtest(
    cfg: AStockConfig,
    req: BacktestRequest,
    *,
    rules: Optional[RuleService] = None,
    progress_cb: Optional[Any] = None,
) -> Dict[str, Any]:
    """Execute portfolio backtest; returns summary dict (also writes outputs)."""
'''))

replacements.append((
'''    codes = select_universe(cfg, req.codes)
    try:
        specs = [reg.get(i) for i in req.rule_ids]
''',
'''    codes = select_universe(cfg, req.codes)

    def _progress(payload: dict) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(payload)
        except Exception:
            pass

    n_codes = len(codes)
    _progress({
        "phase": "prepare",
        "pct": 2.0,
        "current": 0,
        "total": n_codes,
        "message": "准备回测，股票池 %d 只" % n_codes,
        "code": None,
    })

    try:
        specs = [reg.get(i) for i in req.rule_ids]
'''))

replacements.append((
'''    for code in codes:
        try:
            day_raw = store.load_symbol(code)
''',
'''    for idx, code in enumerate(codes):
        # signal phase occupies 5% ~ 85%
        if n_codes > 0:
            pct = 5.0 + 80.0 * (idx / float(n_codes))
        else:
            pct = 5.0
        if idx == 0 or (idx + 1) % 5 == 0 or (idx + 1) == n_codes:
            _progress({
                "phase": "signals",
                "pct": round(pct, 2),
                "current": idx + 1,
                "total": n_codes,
                "message": "计算信号 %d/%d" % (idx + 1, n_codes),
                "code": code,
            })
        try:
            day_raw = store.load_symbol(code)
'''))

replacements.append((
'''    formal_ok, adj_msg = formal_adjustment_ready(factor_series)
''',
'''    _progress({
        "phase": "factors",
        "pct": 86.0,
        "current": n_codes,
        "total": n_codes,
        "message": "校验复权因子",
        "code": None,
    })

    formal_ok, adj_msg = formal_adjustment_ready(factor_series)
'''))

replacements.append((
'''        return {
            "status": "no_go",
            "reason": adj_msg,
            "run_id": run_id,
            "error": adj_msg,
            "hint": meta["hint"],
        }
''',
'''        _progress({
            "phase": "failed",
            "pct": 86.0,
            "current": n_codes,
            "total": n_codes,
            "message": (adj_msg or "")[:200],
            "code": None,
            "run_id": run_id,
        })
        return {
            "status": "no_go",
            "reason": adj_msg,
            "run_id": run_id,
            "error": adj_msg,
            "hint": meta["hint"],
        }
'''))

replacements.append((
'''    bt = PortfolioBacktester(cfg, cal, raw_map, adj_bars_by_code=trade_map)
    result = bt.run(
''',
'''    _progress({
        "phase": "portfolio",
        "pct": 90.0,
        "current": n_codes,
        "total": n_codes,
        "message": "组合回测（信号 %d 条）" % len(events),
        "code": None,
        "n_signals": len(events),
    })

    bt = PortfolioBacktester(cfg, cal, raw_map, adj_bars_by_code=trade_map)
    result = bt.run(
'''))

replacements.append((
'''    paths = write_backtest_csv(out_dir, result, meta=repro)
    write_signals_csv(out_dir / "signals.csv", events)
''',
'''    _progress({
        "phase": "writing",
        "pct": 96.0,
        "current": n_codes,
        "total": n_codes,
        "message": "写入结果文件",
        "code": None,
        "run_id": run_id,
    })

    paths = write_backtest_csv(out_dir, result, meta=repro)
    write_signals_csv(out_dir / "signals.csv", events)
'''))

replacements.append((
'''    summary = {
        "run_id": run_id,
        "status": result.status,
''',
'''    _progress({
        "phase": "done",
        "pct": 100.0,
        "current": n_codes,
        "total": n_codes,
        "message": "完成",
        "code": None,
        "run_id": run_id,
    })

    summary = {
        "run_id": run_id,
        "status": result.status,
'''))

for i, (old, new) in enumerate(replacements):
    if old not in t:
        raise SystemExit("replacement %d not found" % i)
    t = t.replace(old, new, 1)

p.write_text(t, encoding="utf-8")
print("backtest.py patched ok")
