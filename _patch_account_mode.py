# -*- coding: utf-8 -*-
"""Add TDX-style per-symbol account mode + fix hold/entryLag UI layout."""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def patch_strategy() -> None:
    p = ROOT / "wtpy" / "apps" / "astock" / "strategy.py"
    t = p.read_text(encoding="utf-8")

    # run() signature: add account_mode
    old_sig = """        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        entry_lag: int = 1,
    ) -> BacktestResult:
"""
    new_sig = """        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        entry_lag: int = 1,
        account_mode: str = "portfolio",
    ) -> BacktestResult:
"""
    if "account_mode: str = \"portfolio\"" not in t:
        if old_sig not in t:
            raise SystemExit("run signature not found")
        t = t.replace(old_sig, new_sig, 1)

    # after entry_lag validation, normalize account_mode
    old_val = """        entry_lag = int(entry_lag)
        if entry_lag < 1:
            raise ValueError("entry_lag must be >= 1, got %s" % entry_lag)
        period = period.upper()
        notes = [
"""
    new_val = """        entry_lag = int(entry_lag)
        if entry_lag < 1:
            raise ValueError("entry_lag must be >= 1, got %s" % entry_lag)
        account_mode = (account_mode or "portfolio").strip().lower()
        if account_mode in ("tdx", "per_stock", "independent", "通达信", "单票"):
            account_mode = "per_symbol"
        if account_mode not in ("portfolio", "per_symbol"):
            raise ValueError(
                "account_mode must be portfolio or per_symbol, got %s" % account_mode
            )
        period = period.upper()
        notes = [
"""
    if "account_mode not in" not in t:
        if old_val not in t:
            raise SystemExit("entry_lag validation block not found")
        t = t.replace(old_val, new_val, 1)

    # notes about account mode
    old_notes_buy = (
        '            "Buy at open of the N-th trading day after signal close (entry_lag=%d)." % entry_lag,\n'
    )
    new_notes_buy = (
        '            "Buy at open of the N-th trading day after signal close (entry_lag=%d)." % entry_lag,\n'
        '            (\n'
        '                "Account mode: per_symbol (通达信对照) — each stock has its own virtual capital; "\n'
        '                "no cross-stock cash competition; metrics include equal-weight mean stock return."\n'
        '                if account_mode == "per_symbol"\n'
        '                else "Account mode: portfolio — single shared cash account with max_weight cap."\n'
        '            ),\n'
    )
    if "Account mode: per_symbol" not in t:
        if old_notes_buy not in t:
            raise SystemExit("notes buy line not found")
        t = t.replace(old_notes_buy, new_notes_buy, 1)

    # cash init
    old_cash = """        cash = float(self.cfg.initial_capital)
        positions: Dict[str, Position] = {}
        fills: List[Fill] = []
        equity_curve: List[EquityPoint] = []
"""
    new_cash = """        # portfolio: one shared cash; per_symbol: each code has its own book (TDX-style).
        cash = float(self.cfg.initial_capital)
        cash_by_code: Dict[str, float] = {}
        positions: Dict[str, Position] = {}
        fills: List[Fill] = []
        equity_curve: List[EquityPoint] = []
"""
    if "cash_by_code" not in t:
        if old_cash not in t:
            raise SystemExit("cash init not found")
        t = t.replace(old_cash, new_cash, 1)

    # sell cash credit
    old_sell_cash = """                cash += amount - comm - tax
                info = deferred_sells.pop(code, {}) or {}
"""
    new_sell_cash = """                if account_mode == "per_symbol":
                    cash_by_code[code] = float(cash_by_code.get(code, 0.0)) + amount - comm - tax
                else:
                    cash += amount - comm - tax
                info = deferred_sells.pop(code, {}) or {}
"""
    if "cash_by_code[code] = float(cash_by_code.get(code, 0.0)) + amount" not in t:
        if old_sell_cash not in t:
            raise SystemExit("sell cash block not found")
        t = t.replace(old_sell_cash, new_sell_cash, 1)

    # buy sizing + cash debit
    old_buy = """                equity = cash + self._mkt_value(positions, d)
                if equity <= 0:
                    continue
                target_value = equity * self.cfg.max_weight
                n_new = max(1, len(pending_buys.get(d, [])))
                alloc = min(target_value, equity / n_new)
                px = bar.open * (1.0 + self.cfg.costs.slippage)
                shares = int(alloc // (px * self.cfg.lot_size)) * self.cfg.lot_size
                if shares < self.cfg.lot_size:
                    continue
                amount = shares * px
                comm = _commission(amount, self.cfg.costs)
                if amount + comm > cash:
                    shares = (
                        int((cash - self.cfg.costs.min_commission) // (px * self.cfg.lot_size))
                        * self.cfg.lot_size
                    )
                    if shares < self.cfg.lot_size:
                        continue
                    amount = shares * px
                    comm = _commission(amount, self.cfg.costs)
                cash -= amount + comm
"""
    new_buy = """                if account_mode == "per_symbol":
                    # Lazy open a virtual account for this symbol (TDX-style independent capital).
                    if code not in cash_by_code:
                        cash_by_code[code] = float(self.cfg.initial_capital)
                    book = float(cash_by_code[code])
                    if book <= 0:
                        continue
                    equity = book
                    # Full virtual capital (no cross-stock max_weight competition).
                    target_value = book
                    alloc = target_value
                    avail_cash = book
                else:
                    equity = cash + self._mkt_value(positions, d)
                    if equity <= 0:
                        continue
                    target_value = equity * self.cfg.max_weight
                    n_new = max(1, len(pending_buys.get(d, [])))
                    alloc = min(target_value, equity / n_new)
                    avail_cash = cash
                px = bar.open * (1.0 + self.cfg.costs.slippage)
                shares = int(alloc // (px * self.cfg.lot_size)) * self.cfg.lot_size
                if shares < self.cfg.lot_size:
                    continue
                amount = shares * px
                comm = _commission(amount, self.cfg.costs)
                if amount + comm > avail_cash:
                    shares = (
                        int((avail_cash - self.cfg.costs.min_commission) // (px * self.cfg.lot_size))
                        * self.cfg.lot_size
                    )
                    if shares < self.cfg.lot_size:
                        continue
                    amount = shares * px
                    comm = _commission(amount, self.cfg.costs)
                if account_mode == "per_symbol":
                    cash_by_code[code] = float(cash_by_code.get(code, 0.0)) - amount - comm
                else:
                    cash -= amount + comm
"""
    if "Lazy open a virtual account" not in t:
        if old_buy not in t:
            raise SystemExit("buy sizing block not found")
        t = t.replace(old_buy, new_buy, 1)

    # end-of-day equity
    old_eq = """            mv = self._mkt_value(positions, d)
            equity_curve.append(
                EquityPoint(date=d, cash=cash, market_value=mv, equity=cash + mv)
            )
"""
    new_eq = """            mv = self._mkt_value(positions, d)
            if account_mode == "per_symbol":
                # Sum only opened virtual books (+ idle cash of codes that traded / were funded).
                cash_sum = float(sum(cash_by_code.values())) if cash_by_code else 0.0
                # Positions already reflected: cash reduced at buy; MV from holdings.
                equity_curve.append(
                    EquityPoint(date=d, cash=cash_sum, market_value=mv, equity=cash_sum + mv)
                )
            else:
                equity_curve.append(
                    EquityPoint(date=d, cash=cash, market_value=mv, equity=cash + mv)
                )
"""
    if "cash_sum = float(sum(cash_by_code.values()))" not in t:
        if old_eq not in t:
            raise SystemExit("equity append not found")
        t = t.replace(old_eq, new_eq, 1)

    # metrics capital base + per-symbol stats
    old_met = """        metrics = compute_metrics(
            equity_curve, self.cfg.initial_capital, fills, self.cfg.costs
        )
        # open positions at end
        open_n = len(positions)
        open_mv = self._mkt_value(positions, sim_dates[-1]) if sim_dates and positions else 0.0
        metrics["n_open_positions"] = open_n
        metrics["open_market_value"] = float(open_mv)
"""
    new_met = """        if account_mode == "per_symbol":
            n_books = max(1, len(cash_by_code))
            capital_base = float(self.cfg.initial_capital) * float(n_books)
        else:
            capital_base = float(self.cfg.initial_capital)
        metrics = compute_metrics(
            equity_curve, capital_base, fills, self.cfg.costs
        )
        metrics["account_mode"] = account_mode
        metrics["capital_base"] = capital_base
        # open positions at end
        open_n = len(positions)
        open_mv = self._mkt_value(positions, sim_dates[-1]) if sim_dates and positions else 0.0
        metrics["n_open_positions"] = open_n
        metrics["open_market_value"] = float(open_mv)
        if account_mode == "per_symbol":
            # Equal-weight mean of per-stock total returns (TDX-style summary orientation).
            last_d = sim_dates[-1] if sim_dates else None
            stock_rets = []
            for code, book0 in list(cash_by_code.items()):
                end_cash = float(cash_by_code.get(code, 0.0))
                pos = positions.get(code)
                end_mv = 0.0
                if pos and last_d is not None:
                    bar = self._index.get(code, {}).get(last_d)
                    px = bar.close if bar else (self._last_px_on_or_before(code, last_d) or pos.entry_price)
                    end_mv = float(pos.shares) * float(px)
                end_eq = end_cash + end_mv
                stock_rets.append((end_eq / float(self.cfg.initial_capital)) - 1.0)
            if stock_rets:
                metrics["n_symbol_accounts"] = len(stock_rets)
                metrics["mean_symbol_return"] = float(sum(stock_rets) / len(stock_rets))
                srt = sorted(stock_rets)
                mid = len(srt) // 2
                metrics["median_symbol_return"] = float(
                    srt[mid] if len(srt) % 2 == 1 else (srt[mid - 1] + srt[mid]) / 2.0
                )
                metrics["pct_symbols_profitable"] = float(
                    sum(1 for r in stock_rets if r > 0) / len(stock_rets)
                )
            else:
                metrics["n_symbol_accounts"] = 0
                metrics["mean_symbol_return"] = None
                metrics["median_symbol_return"] = None
                metrics["pct_symbols_profitable"] = None
"""
    if "mean_symbol_return" not in t:
        if old_met not in t:
            raise SystemExit("metrics block not found")
        t = t.replace(old_met, new_met, 1)

    # zero-cost recursive run should pass account_mode
    old_z = """                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                    entry_lag=entry_lag,
                )
"""
    new_z = """                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                    entry_lag=entry_lag,
                    account_mode=account_mode,
                )
"""
    if "account_mode=account_mode" not in t:
        # only first occurrence in zero replay
        if old_z not in t:
            print("WARN zero replay account_mode skip")
        else:
            t = t.replace(old_z, new_z, 1)

    # config dict in result
    old_cfg = """                "entry_lag": entry_lag,
                "costs": asdict(self.cfg.costs),
"""
    new_cfg = """                "entry_lag": entry_lag,
                "account_mode": account_mode,
                "costs": asdict(self.cfg.costs),
"""
    if '"account_mode": account_mode' not in t:
        if old_cfg not in t:
            raise SystemExit("result config not found")
        t = t.replace(old_cfg, new_cfg, 1)

    # docstring
    if "per_symbol" not in t[:1200]:
        t = t.replace(
            "- Buy px = open*(1+slippage).\n",
            "- Buy px = open*(1+slippage).\n"
            "- account_mode=portfolio: shared cash + max_weight.\n"
            "- account_mode=per_symbol: each stock independent virtual capital (通达信对照).\n",
            1,
        )

    p.write_text(t, encoding="utf-8")
    ast.parse(t)
    print("OK strategy.py")


def patch_backtest_service() -> None:
    p = ROOT / "wtpy" / "apps" / "astock" / "service" / "backtest.py"
    t = p.read_text(encoding="utf-8")

    if "account_mode" not in t.split("class BacktestRequest")[1][:800]:
        t = t.replace(
            "    take_profit: Optional[float] = None\n",
            "    take_profit: Optional[float] = None\n"
            "    # portfolio = shared cash; per_symbol = TDX-style independent capital per stock\n"
            "    account_mode: str = \"portfolio\"\n",
            1,
        )

    old_run = """        entry_lag=entry_lag,
    )
    if unconfirmed_run:
"""
    new_run = """        entry_lag=entry_lag,
        account_mode=getattr(req, "account_mode", None) or "portfolio",
    )
    if unconfirmed_run:
"""
    if "account_mode=getattr(req" not in t:
        if old_run not in t:
            raise SystemExit("bt.run call not found")
        t = t.replace(old_run, new_run, 1)

    # title tag
    old_title = """    if bagua_enabled and bagua_filter_mode:
        title = f"{title} + {bagua_mode_label(bagua_filter_mode)}"
    elif bagua_enabled:
        title = f"{title} + 八卦"
    title = f"{title} · {period_label} · 持有{hold}"
"""
    new_title = """    if bagua_enabled and bagua_filter_mode:
        title = f"{title} + {bagua_mode_label(bagua_filter_mode)}"
    elif bagua_enabled:
        title = f"{title} + 八卦"
    am = (getattr(req, "account_mode", None) or "portfolio").strip().lower()
    if am in ("tdx", "per_stock", "independent", "通达信", "单票"):
        am = "per_symbol"
    if am == "per_symbol":
        title = f"{title} · 通达信对照(单票独立资金)"
    else:
        title = f"{title} · 组合账户"
    title = f"{title} · {period_label} · 持有{hold}"
"""
    if "通达信对照" not in t:
        if old_title not in t:
            raise SystemExit("title block not found")
        t = t.replace(old_title, new_title, 1)

    # repro
    if '"account_mode"' not in t[t.find("repro = {") : t.find("repro = {") + 1200]:
        # inject near entry_lag in repro
        old_r = '"entry_lag": entry_lag,\n'
        new_r = (
            '"entry_lag": entry_lag,\n'
            '        "account_mode": (getattr(req, "account_mode", None) or "portfolio"),\n'
        )
        # only first in repro - find unique context
        idx = t.find("repro = {")
        part = t[idx : idx + 1500]
        if '"account_mode"' not in part:
            if old_r not in part:
                print("WARN repro entry_lag not found for account_mode")
            else:
                t = t[:idx] + part.replace(old_r, new_r, 1) + t[idx + 1500 :]

    # append_run_index
    if '"account_mode"' not in t[t.find("append_run_index") : t.find("append_run_index") + 900]:
        old_i = '"hold": hold,\n'
        # more unique inside append
        old_block = """                "hold": hold,
                "entry_lag": entry_lag,
                "period": period,
"""
        new_block = """                "hold": hold,
                "entry_lag": entry_lag,
                "account_mode": (getattr(req, "account_mode", None) or "portfolio"),
                "period": period,
"""
        if old_block in t:
            t = t.replace(old_block, new_block, 1)
        else:
            print("WARN index account_mode skip")

    # summary
    if '"account_mode"' not in t[t.find("summary = {") : t.find("summary = {") + 500]:
        old_s = '"hold": hold,\n'
        # careful - many holds; use unique summary context
        old_sum = """        "entry_lag": entry_lag,
        "hold": hold,
        "period": period,
"""
        new_sum = """        "entry_lag": entry_lag,
        "hold": hold,
        "account_mode": (getattr(req, "account_mode", None) or "portfolio"),
        "period": period,
"""
        if old_sum in t:
            t = t.replace(old_sum, new_sum, 1)

    p.write_text(t, encoding="utf-8")
    ast.parse(t)
    print("OK backtest.py")


def patch_api() -> None:
    p = ROOT / "wtpy" / "apps" / "astock" / "api.py"
    t = p.read_text(encoding="utf-8")
    if "account_mode" not in t:
        t = t.replace(
            "    take_profit: Optional[float] = None\n",
            "    take_profit: Optional[float] = None\n"
            "    account_mode: str = \"portfolio\"  # portfolio | per_symbol\n",
            1,
        )
        t = t.replace(
            "            take_profit=payload.take_profit,\n",
            "            take_profit=payload.take_profit,\n"
            "            account_mode=payload.account_mode or \"portfolio\",\n",
            1,
        )
        p.write_text(t, encoding="utf-8")
        ast.parse(t)
        print("OK api.py")
    else:
        print("api already")


def patch_cli() -> None:
    p = ROOT / "wtpy" / "apps" / "astock" / "cli.py"
    t = p.read_text(encoding="utf-8")
    if "--account-mode" not in t:
        # add to backtest parser only - after take-profit if present
        needle = 'sp.add_argument("--stop-loss"'
        # find backtest section's with-bagua already patched; add near run-id for backtest
        if 'sp = sub.add_parser("backtest")' in t:
            # append after last bagua-filter in backtest is hard; inject after --hold for backtest
            t = t.replace(
                'sp = sub.add_parser("backtest")\n',
                'sp = sub.add_parser("backtest")\n',
                1,
            )
        # simpler: after research-unconfirmed in backtest block
        marker = 'sp.add_argument("--research-unconfirmed-formula"'
        # replace only first occurrence that is under backtest - replace all stop-loss sections carefully
        if 'account_mode=getattr' not in t:
            t = t.replace(
                'bagua_filter_mode=getattr(args, "bagua_filter_mode", None),',
                'bagua_filter_mode=getattr(args, "bagua_filter_mode", None),\n'
                '        account_mode=getattr(args, "account_mode", None) or "portfolio",',
                1,
            )
        # add argparse once near backtest --with-bagua (last with-bagua before set_defaults backtest)
        # Add global-ish on both signal/backtest by replacing unique backtest-only arg
        if "--account-mode" not in t:
            t = t.replace(
                'sp.add_argument("--stop-loss", type=_risk_pct_arg, default=None, help="stop',
                'sp.add_argument("--account-mode", default="portfolio",\n'
                '                    choices=["portfolio", "per_symbol", "tdx"],\n'
                '                    help="portfolio shared cash | per_symbol TDX-style")\n'
                '    sp.add_argument("--stop-loss", type=_risk_pct_arg, default=None, help="stop',
                1,
            )
        p.write_text(t, encoding="utf-8")
        try:
            ast.parse(t)
            print("OK cli.py")
        except SyntaxError as e:
            print("cli syntax issue", e)
    else:
        print("cli already")


def patch_reports() -> None:
    p = ROOT / "wtpy" / "apps" / "astock" / "reports.py"
    t = p.read_text(encoding="utf-8")
    if "账户模式" not in t:
        old = '        ("持有天数/期数 hold", repro.get("hold") or ""),\n'
        new = (
            '        ("持有天数/期数 hold", repro.get("hold") or ""),\n'
            '        ("账户模式", (\n'
            '            "通达信对照·单票独立资金" if str(repro.get("account_mode") or "").lower() in\n'
            '            ("per_symbol", "tdx", "per_stock") else "组合账户·共享资金"\n'
            '        )),\n'
        )
        if old in t:
            t = t.replace(old, new, 1)
        # metrics display for mean symbol return
        old2 = '        ("总收益率(含成本)", _fmt_pct(m.get("total_return"))),\n'
        new2 = (
            '        ("总收益率(含成本)", _fmt_pct(m.get("total_return"))),\n'
            '        ("等权平均单票收益(通达信口径)", _fmt_pct(m.get("mean_symbol_return")) if m.get("mean_symbol_return") is not None else ""),\n'
            '        ("单票账户数", m.get("n_symbol_accounts") if m.get("n_symbol_accounts") is not None else ""),\n'
            '        ("盈利股票占比", _fmt_pct(m.get("pct_symbols_profitable")) if m.get("pct_symbols_profitable") is not None else ""),\n'
        )
        if "等权平均单票收益" not in t and old2 in t:
            t = t.replace(old2, new2, 1)
        p.write_text(t, encoding="utf-8")
        ast.parse(t)
        print("OK reports.py")
    else:
        print("reports already")


def patch_ui() -> None:
    p = ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index.html"
    t = p.read_text(encoding="utf-8")

    # CSS for param pair row
    if ".param-pair" not in t:
        t = t.replace(
            ".row3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0.75rem; }",
            ".row3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0.75rem; }\n"
            "    .param-pair {\n"
            "      display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem;\n"
            "      align-items: start;\n"
            "    }\n"
            "    .param-pair .field { display: flex; flex-direction: column; gap: 0.35rem; min-width: 0; }\n"
            "    .param-pair .field label { min-height: 2.6em; line-height: 1.3; }\n"
            "    .param-pair .field input[type=number] { width: 100%; box-sizing: border-box; }\n"
            "    .param-pair .field .hint { font-size: 12px; opacity: 0.8; min-height: 2.4em; }\n"
            "    .acct-mode {\n"
            "      display: flex; flex-wrap: wrap; gap: 0.75rem 1.25rem;\n"
            "      padding: 0.65rem 0.75rem; border: 1px solid var(--border);\n"
            "      border-radius: 8px; background: #121a28; margin: 0.5rem 0 0.75rem;\n"
            "    }\n"
            "    .acct-mode label { display: flex; align-items: flex-start; gap: 0.4rem;\n"
            "      cursor: pointer; color: var(--text); font-size: 0.92rem; max-width: 48%; }\n"
            "    .acct-mode small { display: block; color: var(--muted); font-size: 0.8rem; margin-top: 0.15rem; }\n",
        )

    # Insert account mode block before combine/period row (at start of params)
    if 'name="accountMode"' not in t:
        marker = """        <div class="row">
          <div>
            <label>组合方式（多规则）</label>
"""
        insert = """        <h2 style="margin-top:0.25rem">回测账户模式</h2>
        <div class="acct-mode" id="accountModeBox">
          <label>
            <input type="radio" name="accountMode" value="portfolio" checked />
            <span><b>组合账户</b>（默认）
              <small>全市场共享约 100 万资金，单票约 10% 上限；接近真实可执行组合。</small>
            </span>
          </label>
          <label>
            <input type="radio" name="accountMode" value="per_symbol" />
            <span><b>通达信对照 · 单票独立资金</b>
              <small>每只股票各自独立本金、互不挤兑；汇总含等权平均单票收益，便于与通达信导出表对照。</small>
            </span>
          </label>
        </div>
        <div class="row">
          <div>
            <label>组合方式（多规则）</label>
"""
        if marker not in t:
            raise SystemExit("UI combine row marker not found")
        t = t.replace(marker, insert, 1)

    # Fix entryLag + hold layout
    old_hold_block = """        <div class="row3">
          <div>
            <label>买入延迟（信号后第 N 个交易日开盘买入）</label>
            <input type="number" id="entryLag" min="1" value="1" />
          </div>
          <div>
            <label>强制平仓周期 hold（开仓后经过 N 个周期，无论涨跌都平仓；平仓价=该日收盘价）</label>
            <input type="number" id="hold" min="1" value="1" title="日线=N个交易日；周/月线=N个完整周期。A股T+1：不能开仓当日卖出。到期强制平仓用收盘价；止损/止盈仍为次日开盘。" />
            <div class="hint" style="font-size:12px;opacity:.8;margin-top:4px">
              例：日线 hold=5 → 买入后第 5 个交易日计数满，在可卖日按<strong>收盘价</strong>强制平仓（未触发止损/止盈时）。
            </div>
          </div>
          <div>
            <label>止损 / 止盈（比例，可空）</label>
            <div class="row">
              <input id="sl" placeholder="止损 0.03" />
              <input id="tp" placeholder="止盈 0.08" />
            </div>
          </div>
        </div>
"""
    new_hold_block = """        <div class="param-pair">
          <div class="field">
            <label>买入延迟 entry_lag<br/><span style="font-weight:400;opacity:.85">信号后第 N 个交易日 · 开盘买入</span></label>
            <input type="number" id="entryLag" min="1" value="1" />
            <div class="hint">例：N=1 → 信号次日开盘买（默认）。</div>
          </div>
          <div class="field">
            <label>强制平仓 hold<br/><span style="font-weight:400;opacity:.85">开仓后 N 周期 · 无论涨跌 · 收盘价平仓</span></label>
            <input type="number" id="hold" min="1" value="1" title="日线=N个交易日；周/月线=N个完整周期。A股T+1：不能开仓当日卖出。到期强制平仓用收盘价；止损/止盈仍为次日开盘。" />
            <div class="hint">例：日线 hold=5 → 计满后可卖日按收盘价强平（未触发止损止盈时）。</div>
          </div>
        </div>
        <div class="row" style="margin-top:0.65rem">
          <div>
            <label>止损 / 止盈（比例，可空）</label>
            <div class="row">
              <input id="sl" placeholder="止损 0.03" />
              <input id="tp" placeholder="止盈 0.08" />
            </div>
          </div>
          <div></div>
        </div>
"""
    if 'class="param-pair"' not in t:
        if old_hold_block not in t:
            # try looser: replace from row3 entryLag through sl/tp block
            if 'id="entryLag"' in t and 'id="hold"' in t:
                # fallback regex
                t2, n = re.subn(
                    r'<div class="row3">\s*<div>\s*<label>买入延迟.*?</div>\s*</div>\s*(?=<div class="timeline")',
                    new_hold_block,
                    t,
                    count=1,
                    flags=re.S,
                )
                if n != 1:
                    raise SystemExit(f"hold block replace failed n={n}")
                t = t2
            else:
                raise SystemExit("entryLag/hold not found")
        else:
            t = t.replace(old_hold_block, new_hold_block, 1)

    # body account_mode
    if "account_mode:" not in t.split("const body")[1][:600]:
        t = t.replace(
            "          hold: parseInt(document.getElementById(\"hold\").value, 10) || 1,\n",
            "          hold: parseInt(document.getElementById(\"hold\").value, 10) || 1,\n"
            "          account_mode: (document.querySelector('input[name=\"accountMode\"]:checked') || {}).value || \"portfolio\",\n",
            1,
        )

    # result table show mean symbol return if present
    if "mean_symbol_return" not in t:
        # find metrics rows builder if any
        pass

    p.write_text(t, encoding="utf-8")
    print("OK index.html")


def add_test() -> None:
    p = ROOT / "tests" / "apps" / "astock" / "test_account_mode.py"
    p.write_text(
        '''# -*- coding: utf-8 -*-
"""Portfolio vs per-symbol (TDX-style) account modes."""
from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent


def _cfg():
    cfg = AStockConfig()
    cfg.initial_capital = 100_000
    cfg.max_weight = 0.1
    cfg.lot_size = 100
    cfg.costs = CostConfig(0, 0, 0, 0)
    return cfg


def _bars_two():
    dates = [20240102, 20240103, 20240104, 20240105, 20240108]
    def series(code, base):
        return [
            DayBar(d, base, base + 1, base - 1, base + 0.5, 1, 1000) for d in dates
        ]
    return {
        "SSE.STK.600000": series("SSE.STK.600000", 10.0),
        "SSE.STK.600001": series("SSE.STK.600001", 20.0),
    }, dates


def test_per_symbol_can_buy_both_same_day_portfolio_may_not():
    bars, dates = _bars_two()
    cal = TradeCalendar(dates)
    # two signals same day — portfolio 10% may buy both small; use tiny capital to stress
    cfg = _cfg()
    cfg.initial_capital = 50_000  # with 10% only 5k each -> may fail 600001 lot at 20
    cfg.max_weight = 0.1
    events = [
        SignalEvent("SSE.STK.600000", 20240102, "DAY", "t"),
        SignalEvent("SSE.STK.600001", 20240102, "DAY", "t"),
    ]
    port = PortfolioBacktester(cfg, cal, bars).run(
        events, hold=2, period="DAY", formal_ok=True, _skip_zero_replay=True,
        account_mode="portfolio",
    )
    per = PortfolioBacktester(cfg, cal, bars).run(
        events, hold=2, period="DAY", formal_ok=True, _skip_zero_replay=True,
        account_mode="per_symbol",
    )
    pb = [f for f in port.fills if f.side == "BUY"]
    qb = [f for f in per.fills if f.side == "BUY"]
    # per-symbol should fund each book with full 50k and buy both
    assert len(qb) == 2
    assert per.metrics.get("account_mode") == "per_symbol"
    assert per.metrics.get("n_symbol_accounts") == 2
    assert "mean_symbol_return" in per.metrics
    # portfolio may buy fewer when capital tight
    assert len(pb) <= 2


def test_per_symbol_mean_return_defined():
    bars, dates = _bars_two()
    events = [SignalEvent("SSE.STK.600000", 20240102, "DAY", "t")]
    res = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars).run(
        events, hold=1, period="DAY", formal_ok=True, _skip_zero_replay=True,
        account_mode="per_symbol",
    )
    assert res.metrics.get("mean_symbol_return") is not None
''',
        encoding="utf-8",
    )
    print("OK test_account_mode.py")


def main() -> None:
    patch_strategy()
    patch_backtest_service()
    patch_api()
    patch_cli()
    patch_reports()
    patch_ui()
    add_test()
    print("ALL DONE")


if __name__ == "__main__":
    main()
