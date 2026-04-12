#!/usr/bin/env python3
"""CLI interface for portfolio operations — called by OpenClaw skill."""

import json
import os
import sys

# Set up environment
os.environ.setdefault("PORTFOLIO_DB_PATH", "/share/portfolio-dashboard/portfolio.db")

from jezclaw import db, portfolio, tasks
from jezclaw.ticker_search import search_tickers

# Known ETFs/index funds — get a higher w_min floor (5%) to preserve diversification.
# The optimizer doesn't see inside ETFs, so without this it underweights them
# in favour of direct holdings with higher individual Sharpe ratios.
KNOWN_ETFS = {
    # UK-listed ETFs/index trackers
    "ISF.L", "IUSA.L", "VJPN.L", "VWRP.L", "VUSA.L", "VUKE.L",
    "VMID.L", "VFEM.L", "VWRL.L", "VEVE.L", "CSP1.L", "SWDA.L",
    "EQQQ.L", "RBOT.L", "INRG.L", "IEMG.L",
    # US-listed ETFs
    "SPY", "QQQ", "VTI", "VOO", "IWM", "VEA", "VWO", "BND", "AGG",
    "GLD", "SLV", "ARKK", "XLF", "XLE", "XLK",
}

# Default w_min for ETFs (5% floor vs 2% for individual stocks)
ETF_W_MIN = 0.05


def cmd_portfolio(args):
    """Show portfolio summary."""
    account = args[0] if args else None
    portfolios = db.get_portfolios()
    for p in portfolios:
        if account and p["id"] != account:
            continue
        rows, total = portfolio.get_portfolio_summary(p["id"])
        print(f"\n{p['name']} — £{total:,.0f}")
        print(f"{'Ticker':12s} {'Weight':>7s} {'Value (£)':>12s}")
        print("-" * 35)
        for r in rows:
            print(f"{r['ticker']:12s} {r['weight']:>6.1%}  {r['market_value']:>11,.0f}")


def cmd_risk(args):
    """Show risk metrics."""
    account = args[0] if args else None
    portfolios = db.get_portfolios()
    for p in portfolios:
        if account and p["id"] != account:
            continue
        m = db.get_latest_risk_metrics(p["id"])
        if m:
            print(f"\n{p['name']}:")
            print(f"  Sharpe Ratio:    {m['sharpe_ratio']:.2f}")
            print(f"  Sortino Ratio:   {m['sortino_ratio']:.2f}")
            print(f"  Volatility:      {m['volatility_annual']:.1%}")
            print(f"  Max Drawdown:    {m['max_drawdown']:.1%}")
            print(f"  CVaR (95%):      {m['cvar_95']:.2%}")
        else:
            print(f"\n{p['name']}: no risk metrics available")


def cmd_buy(args):
    """Buy shares: buy <account> <ticker> <shares>"""
    if len(args) < 3:
        print("Usage: buy <sip|ss_isa|gia> <TICKER> <SHARES>")
        return
    account, ticker, shares_str = args[0], args[1].upper(), args[2]
    shares = float(shares_str)

    positions = db.get_positions(account)
    current = next((p["shares"] for p in positions if p["ticker"] == ticker), 0)
    new_total = current + shares

    db.upsert_position(account, ticker, new_total)
    action = "increased" if current > 0 else "added"
    db.log_transaction(account, ticker, action, current, new_total)

    portfolio.fetch_prices()

    print(f"Bought {shares:.2f} {ticker} in {account.upper()}")
    print(f"Position: {current:.2f} -> {new_total:.2f}")


def cmd_sell(args):
    """Sell shares: sell <account> <ticker> <shares|all>"""
    if len(args) < 3:
        print("Usage: sell <sip|ss_isa|gia> <TICKER> <SHARES|all>")
        return
    account, ticker = args[0], args[1].upper()

    positions = db.get_positions(account)
    current = next((p["shares"] for p in positions if p["ticker"] == ticker), 0)

    if current <= 0:
        print(f"No {ticker} position in {account.upper()}")
        return

    if args[2].lower() == "all":
        shares = current
    else:
        shares = float(args[2])

    new_total = current - shares
    if new_total < 0.001:
        db.delete_position(account, ticker)
        db.log_transaction(account, ticker, "removed", current, 0)
        print(f"Sold all {ticker} in {account.upper()} — position closed")
    else:
        db.upsert_position(account, ticker, new_total)
        db.log_transaction(account, ticker, "decreased", current, new_total)
        print(f"Sold {shares:.2f} {ticker} in {account.upper()}")
        print(f"Position: {current:.2f} -> {new_total:.2f}")


def cmd_search(args):
    """Search for a ticker."""
    if not args:
        print("Usage: search <query>")
        return
    query = " ".join(args)
    results = search_tickers(query)
    if not results:
        print(f"No results for '{query}'")
        return
    print(f"Results for '{query}':")
    for r in results[:8]:
        print(f"  {r['symbol']:12s} — {r['name']} ({r['exchange']})")


def cmd_news(args):
    """Check relevant news."""
    news = tasks.fetch_relevant_news()
    if not news:
        print("No relevant news right now.")
        return
    for item in news:
        tags = ", ".join(item["matched"])
        print(f"• {item['title']}")
        print(f"  [{tags}] {item['link']}")
        print()


def cmd_alerts(args):
    """Check smart price alerts."""
    alerts = tasks.check_smart_alerts()
    if not alerts:
        print("No significant price moves right now.")
        return
    for a in alerts:
        impact = f" (£{abs(a['impact_gbp']):,.0f} impact)" if a["market_value"] > 0 else ""
        print(f"  {a['ticker']}: {a['reason']}{impact}")


def cmd_analyse(args):
    """Run market analysis (requires vLLM)."""
    from jezclaw import llm
    context_parts = []
    for p in db.get_portfolios():
        context_parts.append(portfolio.format_portfolio_text(p["id"]))
        m = db.get_latest_risk_metrics(p["id"])
        if m:
            context_parts.append(f"Risk: Sharpe={m['sharpe_ratio']:.2f}, Vol={m['volatility_annual']:.1%}")

    macro = db.get_latest_macro()
    macro_text = "\n".join(f"  {k}: {v['value']:.2f}" for k, v in macro.items())
    context_parts.append(f"Macro:\n{macro_text}")

    prompt = "\n\n".join(context_parts) + "\n\nProvide a concise portfolio analysis and any action items."
    print(llm.chat([{"role": "user", "content": prompt}], max_tokens=1500))


def cmd_prices(args):
    """Fetch latest prices."""
    portfolio.fetch_prices()
    print("Prices updated.")


def cmd_optimize(args):
    """Optimise portfolio via NVIDIA cufolio on DGX Spark."""
    from jezclaw import spark_client

    account = args[0] if args else None
    portfolios_list = db.get_portfolios()
    for p in portfolios_list:
        if account and p["id"] != account:
            continue

        positions = db.get_positions(p["id"])
        if not positions:
            print(f"{p['name']}: no positions")
            continue

        tickers = [pos["ticker"] for pos in positions]
        n_tickers = len(tickers)

        # Build current weights for comparison + turnover awareness
        rows, total_val = portfolio.get_portfolio_summary(p["id"])
        current_weights = {r["ticker"]: r["weight"] for r in rows}

        # Practical constraints for real portfolios:
        # - w_min 2%: don't zero out positions (every holding keeps ≥2%)
        # - w_min 5% for ETFs: ETFs provide diversification the optimizer can't see
        # - w_max 25%: no single-name concentration
        # - existing_weights: turnover-aware — penalises big moves from current
        etfs_in_portfolio = [t for t in tickers if t in KNOWN_ETFS]
        etf_overrides = {t: ETF_W_MIN for t in etfs_in_portfolio} if etfs_in_portfolio else None

        # Get ETF constituent data for look-through scenario modelling
        from jezclaw.etf_holdings import get_all_etf_holdings
        etf_constituents = get_all_etf_holdings(tickers) or None

        print(f"\nOptimising {p['name']} ({n_tickers} tickers) via NVIDIA cufolio...")
        constraints = "w_min=2%, w_max=25%, turnover-aware"
        if etf_overrides:
            constraints += f", ETF floor=5% ({len(etfs_in_portfolio)} ETFs)"
        if etf_constituents:
            constraints += ", ETF look-through"
        print(f"  Constraints: {constraints}")
        try:
            result = spark_client.optimize(
                tickers=tickers,
                w_min=0.02,
                w_max=0.25,
                existing_weights=current_weights,
                w_min_override=etf_overrides,
                etf_constituents=etf_constituents,
            )
        except RuntimeError as e:
            print(f"  Error: {e}")
            continue

        opt_weights = result["weights"]
        metrics = result["metrics"]

        print(f"\nNVIDIA Optimizer Result — {p['name']}")
        print(f"  Solver: {result['solver_used']}  |  Scenarios: {result['num_scenarios']}")
        print(f"  Expected return: {metrics['expected_return']:.4f}")
        print(f"  CVaR (95%):      {metrics['cvar']:.4f}")
        print(f"  Solve time:      {metrics['solve_time_seconds']:.1f}s")
        print(f"\n{'Ticker':12s} {'Current':>8s} {'Optimal':>8s} {'Delta':>8s}  Action")
        print("-" * 52)

        for ticker in sorted(set(list(current_weights.keys()) + list(opt_weights.keys()))):
            cw = current_weights.get(ticker, 0)
            ow = opt_weights.get(ticker, 0)
            delta = ow - cw
            if abs(delta) < 0.005:
                action = "HOLD"
            elif delta > 0:
                action = "BUY"
            else:
                action = "SELL"
            print(f"{ticker:12s} {cw:>7.1%} {ow:>7.1%} {delta:>+7.1%}  {action}")

        if result.get("cash", 0) > 0.005:
            print(f"{'CASH':12s} {'':>8s} {result['cash']:>7.1%}")

        # Store result
        db.insert_optimization_result(
            p["id"], "on_demand", opt_weights,
            result.get("cash", 0), metrics["expected_return"],
            metrics["cvar"], result["solver_used"],
        )


def cmd_stress_test(args):
    """Run stress test scenario via NVIDIA cufolio on DGX Spark."""
    from jezclaw import spark_client

    if not args:
        print("Usage: stress-test <scenario> [sip|ss_isa|gia]")
        print(f"Scenarios: {', '.join(spark_client.STRESS_SCENARIOS)}")
        # Auto-suggest based on macro conditions
        macro = db.get_latest_macro()
        if macro:
            vix = macro.get("^VIX", {}).get("value", 0)
            oil = macro.get("CL=F", {}).get("value", 0)
            tnx = macro.get("^TNX", {}).get("value", 0)
            suggestions = []
            if vix > 25:
                suggestions.append("recession (VIX elevated)")
            if oil > 90:
                suggestions.append("gulf-war (oil above $90)")
            if tnx > 4.5:
                suggestions.append("rate-hike (10Y yield above 4.5%)")
            if suggestions:
                print(f"\nSuggested based on current conditions: {', '.join(suggestions)}")
        return

    scenario = args[0].lower()
    account = args[1] if len(args) > 1 else None

    portfolios_list = db.get_portfolios()
    for p in portfolios_list:
        if account and p["id"] != account:
            continue

        positions = db.get_positions(p["id"])
        if not positions:
            continue

        tickers = [pos["ticker"] for pos in positions]
        n_tickers = len(tickers)

        print(f"\nStress testing {p['name']} — scenario: {scenario}...")
        try:
            result = spark_client.stress_test(
                tickers=tickers, scenario_name=scenario,
                w_min=0.02, w_max=0.25,
            )
        except RuntimeError as e:
            print(f"  Error: {e}")
            continue

        print(f"\nScenario: {result['scenario_description']}")

        base = result["base_case"]
        stress = result["stress_case"]
        print(f"\n{'':30s} {'Base':>10s} {'Stressed':>10s}")
        print("-" * 55)
        print(f"{'Expected return':30s} {base['metrics']['expected_return']:>10.4f} {stress['metrics']['expected_return']:>10.4f}")
        print(f"{'CVaR (95%)':30s} {base['metrics']['cvar']:>10.4f} {stress['metrics']['cvar']:>10.4f}")

        hedges = result.get("hedge_trades", [])
        if hedges:
            print(f"\nRecommended hedge trades:")
            for h in hedges[:10]:
                print(f"  {h['action']:4s} {h['ticker']:12s} ({h['delta_weight']:+.1%})")
        else:
            print("\nNo significant rebalancing needed under this scenario.")

        # Store stress result
        db.insert_optimization_result(
            p["id"], "stress_test", stress["weights"],
            stress.get("cash", 0), stress["metrics"]["expected_return"],
            stress["metrics"]["cvar"], stress.get("solver_used", ""),
            stress_scenario=scenario,
        )


def cmd_frontier(args):
    """Show efficient frontier via NVIDIA cufolio on DGX Spark."""
    from jezclaw import spark_client

    account = args[0] if args else None
    portfolios_list = db.get_portfolios()
    for p in portfolios_list:
        if account and p["id"] != account:
            continue

        positions = db.get_positions(p["id"])
        if not positions:
            continue

        tickers = [pos["ticker"] for pos in positions]

        print(f"\nComputing efficient frontier for {p['name']}...")
        try:
            result = spark_client.frontier(tickers=tickers, num_points=12)
        except RuntimeError as e:
            print(f"  Error: {e}")
            continue

        print(f"\n{'Risk Aversion':>14s} {'Return':>10s} {'CVaR':>10s} {'Top Holdings':30s}")
        print("-" * 68)
        for pt in result.get("points", []):
            top = sorted(pt["weights"].items(), key=lambda x: -x[1])[:3]
            top_str = ", ".join(f"{t}:{w:.0%}" for t, w in top)
            print(f"{pt['risk_aversion']:>14.4f} {pt['expected_return']:>10.4f} {pt['cvar']:>10.4f} {top_str}")


def cmd_backtest(args):
    """Backtest portfolio via NVIDIA cufolio on DGX Spark."""
    from jezclaw import spark_client

    account = args[0] if args else "sip"
    use_optimal = "optimal" in args

    positions = db.get_positions(account)
    if not positions:
        print(f"No positions for {account}")
        return

    tickers = [pos["ticker"] for pos in positions]

    if use_optimal:
        opt = db.get_latest_optimization(account)
        if not opt:
            print("No optimization result available — run optimize first")
            return
        weights = opt["weights"]
        cash = opt.get("cash", 0)
        label = "Optimal"
    else:
        rows, total_val = portfolio.get_portfolio_summary(account)
        weights = {r["ticker"]: r["weight"] for r in rows}
        cash = 0.0
        label = "Current"

    print(f"\nBacktesting {label} portfolio for {account}...")
    try:
        result = spark_client.backtest(tickers=tickers, weights=weights, cash=cash)
    except RuntimeError as e:
        print(f"  Error: {e}")
        return

    print(f"\n{label} Portfolio Backtest:")
    print(f"  Sharpe Ratio:      {result['sharpe_ratio']:.3f}")
    print(f"  Sortino Ratio:     {result['sortino_ratio']:.3f}")
    print(f"  Max Drawdown:      {result['max_drawdown']:.1%}")
    print(f"  Cumulative Return: {result['cumulative_return']:.1%}")
    print(f"  Volatility:        {result['volatility']:.1%}")


def cmd_risk_history(args):
    """Show risk metrics trend over time."""
    account = args[0] if args else None
    days = 30
    if len(args) > 1:
        try:
            days = int(args[1])
        except ValueError:
            pass

    portfolios = db.get_portfolios()
    for p in portfolios:
        if account and p["id"] != account:
            continue
        history = db.get_risk_history(p["id"], days=days)
        if not history:
            print(f"{p['name']}: no risk history available")
            continue

        print(f"\n{p['name']} — Risk History (last {days} days)")
        print(f"{'Date':12s} {'Value (£)':>12s} {'Sharpe':>8s} {'Vol':>8s} {'MDD':>8s} {'CVaR':>8s}")
        print("-" * 60)
        for r in history[:20]:
            val = f"£{r['total_value']:,.0f}" if r.get("total_value") else "—"
            sharpe = f"{r['sharpe_ratio']:.2f}" if r.get("sharpe_ratio") is not None else "—"
            vol = f"{r['volatility_annual']:.1%}" if r.get("volatility_annual") is not None else "—"
            mdd = f"{r['max_drawdown']:.1%}" if r.get("max_drawdown") is not None else "—"
            cvar = f"{r['cvar_95']:.2%}" if r.get("cvar_95") is not None else "—"
            print(f"{r['date']:12s} {val:>12s} {sharpe:>8s} {vol:>8s} {mdd:>8s} {cvar:>8s}")


def cmd_trades(args):
    """Show recent trade history."""
    account = args[0] if args else None
    limit = 20
    if len(args) > 1:
        try:
            limit = int(args[1])
        except ValueError:
            pass

    trades = db.get_transaction_log(portfolio_id=account, limit=limit)
    if not trades:
        print("No trade history available.")
        return

    print(f"\nRecent Trades{' — ' + account.upper() if account else ''}")
    print(f"{'Date':20s} {'Account':8s} {'Action':10s} {'Ticker':12s} {'Before':>8s} {'After':>8s} {'Delta':>8s}")
    print("-" * 72)
    for t in trades:
        delta = t.get("shares_delta", (t.get("shares_after", 0) or 0) - (t.get("shares_before", 0) or 0))
        print(f"{t['logged_at'][:16]:20s} {t['portfolio_id']:8s} {t['action']:10s} "
              f"{t['ticker']:12s} {t.get('shares_before', 0):>8.1f} {t.get('shares_after', 0):>8.1f} {delta:>+8.1f}")


def cmd_macro(args):
    """Show current macro indicators and recent trend."""
    macro = db.get_latest_macro()
    if not macro:
        print("No macro data available.")
        return

    print("\nMacro Indicators (latest)")
    print(f"{'Indicator':15s} {'Value':>10s} {'Date':>12s}")
    print("-" * 40)

    labels = {
        "^VIX": "VIX (Fear)",
        "GC=F": "Gold ($/oz)",
        "CL=F": "Oil ($/bbl)",
        "^TNX": "US 10Y (%)",
        "GBPUSD=X": "GBP/USD",
    }
    for ind, data in sorted(macro.items()):
        label = labels.get(ind, ind)
        print(f"{label:15s} {data['value']:>10.2f} {data['date']:>12s}")

    # Show VIX context
    vix = macro.get("^VIX", {}).get("value")
    if vix:
        if vix > 30:
            print(f"\n⚠ VIX at {vix:.1f} — high fear, consider stress-test scenarios")
        elif vix > 20:
            print(f"\nVIX at {vix:.1f} — elevated caution")
        else:
            print(f"\nVIX at {vix:.1f} — calm markets")


def cmd_drift(args):
    """Show portfolio drift from optimizer targets."""
    account = args[0] if args else None
    portfolios = db.get_portfolios()
    for p in portfolios:
        if account and p["id"] != account:
            continue

        opt = db.get_latest_optimization(p["id"])
        if not opt:
            print(f"{p['name']}: no optimizer targets — run optimize first")
            continue

        rows, total = portfolio.get_portfolio_summary(p["id"])
        current_weights = {r["ticker"]: r["weight"] for r in rows}
        target_weights = opt["weights"]

        print(f"\n{p['name']} — Drift from Target ({opt['run_date']})")
        print(f"{'Ticker':12s} {'Current':>8s} {'Target':>8s} {'Drift':>8s}")
        print("-" * 40)

        total_drift = 0
        for ticker in sorted(set(list(current_weights) + list(target_weights))):
            cw = current_weights.get(ticker, 0)
            tw = target_weights.get(ticker, 0)
            drift = cw - tw
            total_drift += abs(drift)
            if abs(drift) > 0.005:
                print(f"{ticker:12s} {cw:>7.1%} {tw:>7.1%} {drift:>+7.1%}")

        print(f"\nTotal drift: {total_drift:.1%}")
        if total_drift > 0.20:
            print("Significant drift — consider rebalancing")
        elif total_drift > 0.10:
            print("Moderate drift — monitor")
        else:
            print("Within tolerance")


def cmd_value_history(args):
    """Show portfolio value over time."""
    account = args[0] if args else None
    days = 30
    if len(args) > 1:
        try:
            days = int(args[1])
        except ValueError:
            pass

    portfolios = db.get_portfolios()
    for p in portfolios:
        if account and p["id"] != account:
            continue
        history = db.get_portfolio_total_value_history(p["id"], days=days)
        if not history:
            print(f"{p['name']}: no value history available")
            continue

        print(f"\n{p['name']} — Value History (last {days} days)")
        print(f"{'Date':12s} {'Value (£)':>12s} {'Change':>10s}")
        print("-" * 38)
        prev = None
        for r in history[:30]:
            val = r["total_value"]
            if prev is not None:
                change = f"{(val - prev) / prev:+.2%}"
            else:
                change = "—"
            print(f"{r['date']:12s} £{val:>11,.0f} {change:>10s}")
            prev = val


def cmd_last_optimize(args):
    """Show last optimization result from the database."""
    account = args[0] if args else None
    portfolios_list = db.get_portfolios()
    for p in portfolios_list:
        if account and p["id"] != account:
            continue
        opt = db.get_latest_optimization(p["id"])
        if not opt:
            print(f"{p['name']}: no optimization results")
            continue
        print(f"\n{p['name']} — Last optimised: {opt['run_date']} ({opt['run_type']})")
        print(f"  Solver: {opt.get('solver', '?')}  |  CVaR: {opt.get('cvar', 0):.4f}")
        print(f"  {'Ticker':12s} {'Weight':>8s}")
        print(f"  {'-' * 22}")
        for ticker, w in sorted(opt["weights"].items(), key=lambda x: -x[1]):
            print(f"  {ticker:12s} {w:>7.1%}")


def cmd_look_through(args):
    """Show true per-stock exposure including what's inside ETFs.

    Decomposes ETFs into constituents so you can see your real exposure.
    The ETF itself remains the tradeable unit — this is for awareness only.
    """
    from jezclaw.etf_holdings import compute_look_through, get_overlap_warnings

    account = args[0] if args else None
    portfolios = db.get_portfolios()
    for p in portfolios:
        if account and p["id"] != account:
            continue

        rows, total_val = portfolio.get_portfolio_summary(p["id"])
        if not rows:
            print(f"{p['name']}: no positions")
            continue

        current_weights = {r["ticker"]: r["weight"] for r in rows}
        exposure = compute_look_through(current_weights)

        # Overlap warnings first
        overlaps = get_overlap_warnings(current_weights)
        if overlaps:
            print(f"\n{p['name']} — OVERLAP WARNINGS (held directly AND through ETFs)")
            print(f"{'Stock':12s} {'Direct':>8s} {'via ETF':>8s} {'TRUE':>8s}  Source")
            print("-" * 55)
            for o in overlaps:
                etf_list = ", ".join(o["etfs"])
                print(f"{o['ticker']:12s} {o['direct_weight']:>7.1%} {o['etf_weight']:>7.1%} "
                      f"{o['total_weight']:>7.1%}  {etf_list}")

        # Full look-through
        print(f"\n{p['name']} — TRUE EXPOSURE (look-through)")
        print(f"  Portfolio value: £{total_val:,.0f}")

        # Sort by total exposure
        sorted_exp = sorted(exposure.items(), key=lambda x: -x[1]["total"])

        # Direct holdings
        directs = [(t, e) for t, e in sorted_exp if e["direct"] > 0 and t not in KNOWN_ETFS]
        etfs = [(t, e) for t, e in sorted_exp if t in KNOWN_ETFS]
        via_only = [(t, e) for t, e in sorted_exp if e["direct"] == 0 and e["total"] > 0.001]

        if directs:
            print(f"\n  Direct holdings:")
            for t, e in directs[:15]:
                etf_extra = f" (+{sum(e['via_etf'].values()):.1%} via ETFs)" if e["via_etf"] else ""
                print(f"    {t:12s} {e['direct']:>6.1%}{etf_extra}")

        if etfs:
            print(f"\n  ETFs (tradeable units — you can't adjust what's inside):")
            for t, e in etfs:
                residual = e.get("residual", 0)
                top_holdings = list(get_overlap_warnings({t: e["direct"]}) or [])
                print(f"    {t:12s} {e['direct']:>6.1%}  (top 20 cover ~{1-residual/e['direct']:.0%})")

        if via_only:
            print(f"\n  Hidden exposure (only via ETFs, not held directly):")
            for t, e in via_only[:10]:
                sources = ", ".join(f"{k}:{v:.2%}" for k, v in e["via_etf"].items())
                print(f"    {t:12s} {e['total']:>6.2%}  ({sources})")


def cmd_consider(args):
    """Evaluate adding or changing a ticker — runs optimizer with it included.

    Usage: consider <TICKER> [sip|ss_isa|gia]
    Examples:
      consider AMZN          — what if I added Amazon to my SIPP?
      consider VWRP.L        — should I increase my global ETF?
      consider AMZN ss_isa   — evaluate for ISA instead
    """
    from jezclaw import spark_client

    if not args:
        print("Usage: consider <TICKER> [sip|ss_isa|gia]")
        print("Evaluates what the optimizer thinks of a ticker in your portfolio.")
        print("Examples:")
        print("  consider AMZN         — what if I added Amazon?")
        print("  consider VWRP.L       — should I hold more VWRP?")
        return

    ticker = args[0].upper()
    account = args[1] if len(args) > 1 else "sip"

    positions = db.get_positions(account)
    if not positions:
        print(f"No positions for {account}")
        return

    existing_tickers = [pos["ticker"] for pos in positions]
    rows, total_val = portfolio.get_portfolio_summary(account)
    current_weights = {r["ticker"]: r["weight"] for r in rows}

    is_new = ticker not in existing_tickers
    if is_new:
        # Add new ticker with 0 current weight
        all_tickers = existing_tickers + [ticker]
        current_weights[ticker] = 0.0
        print(f"\nEvaluating NEW position: {ticker} for {account.upper()}")
    else:
        all_tickers = existing_tickers
        print(f"\nEvaluating EXISTING position: {ticker} ({current_weights[ticker]:.1%}) in {account.upper()}")

    is_etf = ticker in KNOWN_ETFS
    if is_etf:
        print(f"  Recognised as ETF — 5% floor applied")

    # Build ETF overrides for all ETFs in the combined portfolio
    etf_overrides = {t: ETF_W_MIN for t in all_tickers if t in KNOWN_ETFS} or None

    print(f"  Running optimizer with {len(all_tickers)} tickers...")
    try:
        # Run WITH the ticker
        result_with = spark_client.optimize(
            tickers=all_tickers,
            w_min=0.02,
            w_max=0.25,
            existing_weights=current_weights,
            w_min_override=etf_overrides,
        )
    except RuntimeError as e:
        print(f"  Error: {e}")
        return

    opt_weights = result_with["weights"]
    metrics_with = result_with["metrics"]
    recommended_weight = opt_weights.get(ticker, 0.0)

    # Run WITHOUT the ticker for comparison (only if new)
    if is_new:
        try:
            result_without = spark_client.optimize(
                tickers=existing_tickers,
                w_min=0.02,
                w_max=0.25,
                existing_weights={t: w for t, w in current_weights.items() if t != ticker},
                w_min_override={t: ETF_W_MIN for t in existing_tickers if t in KNOWN_ETFS} or None,
            )
            metrics_without = result_without["metrics"]
        except RuntimeError:
            result_without = None
            metrics_without = None
    else:
        result_without = None
        metrics_without = None

    # Report
    print(f"\n{'═' * 55}")
    print(f"NVIDIA Optimizer Assessment — {ticker}")
    print(f"{'═' * 55}")

    if is_new:
        print(f"  Recommended allocation: {recommended_weight:.1%}")
        if recommended_weight > 0.10:
            print(f"  Verdict: STRONG BUY — optimizer wants significant allocation")
        elif recommended_weight > 0.05:
            print(f"  Verdict: BUY — optimizer sees value, moderate allocation")
        elif recommended_weight > 0.02:
            print(f"  Verdict: SMALL POSITION — adds marginal diversification")
        else:
            print(f"  Verdict: SKIP — optimizer assigns minimum weight only")

        if metrics_without:
            ret_delta = metrics_with["expected_return"] - metrics_without["expected_return"]
            cvar_delta = metrics_with["cvar"] - metrics_without["cvar"]
            print(f"\n  Portfolio impact:")
            print(f"    Expected return: {ret_delta:+.4f} ({'better' if ret_delta > 0 else 'worse'})")
            print(f"    CVaR (95%):      {cvar_delta:+.4f} ({'more risk' if cvar_delta > 0 else 'less risk'})")
            if ret_delta > 0 and cvar_delta <= 0:
                print(f"    → Improves BOTH return and risk — strong addition")
            elif ret_delta > 0 and cvar_delta > 0:
                print(f"    → Higher return but also higher risk — consider carefully")
            elif ret_delta <= 0 and cvar_delta < 0:
                print(f"    → Lower return but also lower risk — pure hedge")
            else:
                print(f"    → Lower return AND higher risk — poor addition")
    else:
        delta = recommended_weight - current_weights[ticker]
        print(f"  Current weight:     {current_weights[ticker]:.1%}")
        print(f"  Optimal weight:     {recommended_weight:.1%}")
        print(f"  Recommended change: {delta:+.1%}")
        if abs(delta) < 0.005:
            print(f"  Verdict: HOLD — current allocation is near optimal")
        elif delta > 0.05:
            print(f"  Verdict: INCREASE — optimizer wants significantly more")
        elif delta > 0:
            print(f"  Verdict: SLIGHT INCREASE")
        elif delta < -0.05:
            print(f"  Verdict: REDUCE — optimizer wants significantly less")
        else:
            print(f"  Verdict: SLIGHT TRIM")

    # Show how other positions shift
    print(f"\n  Top position changes if {ticker} {'added' if is_new else 'reweighted'}:")
    shifts = []
    for t in sorted(set(list(current_weights) + list(opt_weights))):
        if t == ticker:
            continue
        cw = current_weights.get(t, 0)
        ow = opt_weights.get(t, 0)
        d = ow - cw
        if abs(d) > 0.005:
            shifts.append((t, cw, ow, d))

    shifts.sort(key=lambda x: abs(x[3]), reverse=True)
    for t, cw, ow, d in shifts[:8]:
        action = "↑" if d > 0 else "↓"
        print(f"    {t:12s} {cw:>6.1%} → {ow:>6.1%} ({d:+.1%}) {action}")


COMMANDS = {
    "portfolio": cmd_portfolio,
    "risk": cmd_risk,
    "risk-history": cmd_risk_history,
    "buy": cmd_buy,
    "sell": cmd_sell,
    "search": cmd_search,
    "news": cmd_news,
    "alerts": cmd_alerts,
    "analyse": cmd_analyse,
    "analyze": cmd_analyse,
    "prices": cmd_prices,
    "optimize": cmd_optimize,
    "optimise": cmd_optimize,
    "stress-test": cmd_stress_test,
    "frontier": cmd_frontier,
    "backtest": cmd_backtest,
    "last-optimize": cmd_last_optimize,
    "consider": cmd_consider,
    "evaluate": cmd_consider,
    "look-through": cmd_look_through,
    "exposure": cmd_look_through,
    "trades": cmd_trades,
    "macro": cmd_macro,
    "drift": cmd_drift,
    "value-history": cmd_value_history,
}


def main():
    if len(sys.argv) < 2:
        print("Available commands:", ", ".join(COMMANDS.keys()))
        return

    cmd = sys.argv[1].lower()
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print("Available:", ", ".join(COMMANDS.keys()))
        return

    COMMANDS[cmd](sys.argv[2:])


if __name__ == "__main__":
    main()
