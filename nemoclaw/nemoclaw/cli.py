#!/usr/bin/env python3
"""CLI interface for portfolio operations — called by OpenClaw skill."""

import json
import os
import sys

# Set up environment
os.environ.setdefault("PORTFOLIO_DB_PATH", "/share/portfolio-dashboard/portfolio.db")

from nemoclaw import db, portfolio, tasks
from nemoclaw.ticker_search import search_tickers


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
        print("Usage: buy <sip|ss_isa> <TICKER> <SHARES>")
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
        print("Usage: sell <sip|ss_isa> <TICKER> <SHARES|all>")
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
    from nemoclaw import llm
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


COMMANDS = {
    "portfolio": cmd_portfolio,
    "risk": cmd_risk,
    "buy": cmd_buy,
    "sell": cmd_sell,
    "search": cmd_search,
    "news": cmd_news,
    "alerts": cmd_alerts,
    "analyse": cmd_analyse,
    "analyze": cmd_analyse,
    "prices": cmd_prices,
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
