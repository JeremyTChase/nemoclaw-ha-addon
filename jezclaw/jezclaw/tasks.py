"""Scheduled tasks — intelligent alerts, news monitoring, and analysis."""

import logging
import hashlib
from datetime import datetime, timedelta

import feedparser
import requests

from . import db, llm, portfolio

logger = logging.getLogger("jezclaw.tasks")


# ─── Alert deduplication ───────────────────────────────────────────

def _get_alert_hash(content):
    """Hash alert content for dedup."""
    return hashlib.md5(content.encode()).hexdigest()[:12]


def _was_already_alerted(alert_hash):
    """Check if we already sent this alert today."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with db.get_conn() as conn:
        # Create tracking table if needed
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_history (
                hash TEXT NOT NULL,
                date TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (hash, date)
            )
        """)
        row = conn.execute(
            "SELECT 1 FROM alert_history WHERE hash=? AND date=?",
            (alert_hash, today),
        ).fetchone()
        return row is not None


def _mark_alerted(alert_hash):
    """Mark an alert as sent today."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    now = datetime.utcnow().isoformat()
    with db.get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_history (
                hash TEXT NOT NULL,
                date TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (hash, date)
            )
        """)
        conn.execute(
            "INSERT OR IGNORE INTO alert_history (hash, date, sent_at) VALUES (?, ?, ?)",
            (alert_hash, today, now),
        )
        # Clean up old entries (keep 7 days)
        cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        conn.execute("DELETE FROM alert_history WHERE date < ?", (cutoff,))


# ─── Price updates (silent, no notification) ───────────────────────

def run_price_update():
    """Fetch prices silently — no Telegram notification."""
    if portfolio.is_market_hours():
        portfolio.fetch_prices()


# ─── Smart price alerts ───────────────────────────────────────────

def check_smart_alerts():
    """Intelligent alert system — only fires when something genuinely new happens.

    Rules:
    1. Only alert on moves > 5% daily or > 10% weekly (higher thresholds)
    2. Deduplicate — don't repeat the same alert in the same day
    3. Use LLM to assess if the move is actually noteworthy for the portfolio
    4. Include portfolio impact (£ value at risk)

    Returns list of alert dicts or empty list.
    """
    if not portfolio.is_market_hours():
        return []

    alerts = []
    tickers = db.get_all_tickers()

    # Get portfolio context for impact calculation
    all_summaries = {}
    for p in db.get_portfolios():
        rows, total = portfolio.get_portfolio_summary(p["id"])
        for r in rows:
            if r["ticker"] not in all_summaries:
                all_summaries[r["ticker"]] = {"market_value": 0, "weight": 0}
            all_summaries[r["ticker"]]["market_value"] += r["market_value"]
            all_summaries[r["ticker"]]["weight"] = r["weight"]

    with db.get_conn() as conn:
        for ticker in tickers:
            rows = conn.execute(
                "SELECT close, date FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 6",
                (ticker,),
            ).fetchall()
            if len(rows) < 2:
                continue

            today_price = rows[0]["close"]
            yesterday_price = rows[1]["close"]
            daily_pct = (today_price - yesterday_price) / yesterday_price

            weekly_pct = None
            if len(rows) >= 6:
                week_price = rows[5]["close"]
                weekly_pct = (today_price - week_price) / week_price

            # Higher thresholds — only genuinely big moves
            significant = False
            reason = ""

            if abs(daily_pct) >= 0.05:  # 5% daily move
                significant = True
                reason = f"{daily_pct:+.1%} today"
            elif weekly_pct and abs(weekly_pct) >= 0.10:  # 10% weekly move
                significant = True
                reason = f"{weekly_pct:+.1%} this week"

            if not significant:
                continue

            # Dedup check
            alert_key = f"{ticker}_{reason}_{datetime.utcnow().strftime('%Y-%m-%d')}"
            alert_hash = _get_alert_hash(alert_key)
            if _was_already_alerted(alert_hash):
                continue

            # Calculate portfolio impact
            holding = all_summaries.get(ticker, {})
            mv = holding.get("market_value", 0)
            impact = mv * daily_pct if mv else 0

            alerts.append({
                "ticker": ticker,
                "daily_pct": daily_pct,
                "weekly_pct": weekly_pct,
                "reason": reason,
                "market_value": mv,
                "impact_gbp": impact,
                "hash": alert_hash,
            })

    return alerts


def run_smart_alerts():
    """Check for significant moves, get LLM analysis, return message or None."""
    alerts = check_smart_alerts()
    if not alerts:
        return None

    # Build alert summary
    alert_lines = []
    for a in alerts:
        impact_str = f" (£{abs(a['impact_gbp']):,.0f} impact)" if a["market_value"] > 0 else ""
        alert_lines.append(f"  {a['ticker']}: {a['reason']}{impact_str}")

    alert_text = "\n".join(alert_lines)

    # Get LLM assessment — is this actually worth bothering Jeremy about?
    context_parts = []
    for p in db.get_portfolios():
        context_parts.append(portfolio.format_portfolio_text(p["id"]))

    prompt = (
        f"Portfolio:\n{''.join(context_parts)}\n\n"
        f"Price moves detected:\n{alert_text}\n\n"
        "Assess these moves:\n"
        "1. What's driving each move? (1 sentence each)\n"
        "2. Is this a genuine concern or opportunity for THIS portfolio? Rate: IGNORE / WATCH / ACT\n"
        "3. If ACT: what should Jeremy consider doing?\n\n"
        "Be concise — this is a Telegram alert. Only include moves rated WATCH or ACT."
    )

    analysis = llm.chat([{"role": "user", "content": prompt}], max_tokens=800)

    # If LLM says everything is IGNORE, don't bother sending
    if analysis and "IGNORE" in analysis and "WATCH" not in analysis and "ACT" not in analysis:
        logger.info("LLM assessed all moves as IGNORE — not alerting")
        for a in alerts:
            _mark_alerted(a["hash"])
        return None

    # Mark as sent and return
    for a in alerts:
        _mark_alerted(a["hash"])

    db.insert_agent_log("smart_alert", alert_text[:200], analysis, "warning")
    return f"📊 Market Alert\n\n{alert_text}\n\n{analysis}"


# ─── News monitoring ──────────────────────────────────────────────

NEWS_FEEDS = [
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://feeds.reuters.com/reuters/businessNews",
]


def fetch_relevant_news():
    """Fetch news and filter for items relevant to portfolio holdings.

    Returns list of relevant headlines with links.
    """
    tickers = db.get_all_tickers()

    # Build keyword list from tickers and company names
    keywords = set()
    for t in tickers:
        keywords.add(t.replace(".L", "").lower())
    # Add common names for major holdings
    name_map = {
        "RR.L": ["rolls-royce", "rolls royce"],
        "BARC.L": ["barclays"],
        "DGE.L": ["diageo"],
        "GSK.L": ["gsk", "glaxo"],
        "HSBA.L": ["hsbc"],
        "BAB.L": ["babcock"],
        "ISF.L": ["ftse 100", "ftse100"],
        "LSEG.L": ["london stock exchange", "lseg"],
        "AAL.L": ["anglo american"],
        "PSN.L": ["persimmon"],
        "NVDA": ["nvidia"],
        "TSLA": ["tesla"],
        "DELL": ["dell"],
        "INTC": ["intel"],
        "NVO": ["novo nordisk"],
        "ASML": ["asml"],
        "DNN": ["denison"],
    }
    for ticker, names in name_map.items():
        if ticker in tickers:
            keywords.update(n.lower() for n in names)

    # Also add macro keywords
    keywords.update(["interest rate", "bank of england", "federal reserve", "fed rate",
                     "oil price", "gold price", "middle east", "tariff", "trade war",
                     "recession", "inflation", "ftse", "s&p 500", "nasdaq"])

    relevant = []
    seen_titles = set()

    for feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:20]:
                title = entry.get("title", "").lower()
                summary = entry.get("summary", "").lower()
                link = entry.get("link", "")

                if title in seen_titles:
                    continue

                # Check if any keyword matches
                text = f"{title} {summary}"
                matched = [k for k in keywords if k in text]

                if matched:
                    seen_titles.add(title)
                    relevant.append({
                        "title": entry.get("title", ""),
                        "link": link,
                        "matched": matched[:3],
                        "source": feed_url.split("/")[2],
                    })
        except Exception as e:
            logger.warning(f"Feed fetch failed ({feed_url}): {e}")

    return relevant[:10]  # Cap at 10


def run_news_check():
    """Check news and alert if anything relevant to holdings is found.

    Only alerts once per news item (deduped by title hash).
    """
    news = fetch_relevant_news()
    if not news:
        return None

    # Filter out already-alerted news
    new_items = []
    for item in news:
        h = _get_alert_hash(item["title"])
        if not _was_already_alerted(h):
            new_items.append(item)
            _mark_alerted(h)

    if not new_items:
        return None

    # Format for Telegram
    lines = ["📰 Relevant News\n"]
    for item in new_items:
        tags = ", ".join(item["matched"])
        lines.append(f"• {item['title']}\n  [{tags}] {item['link']}")

    message = "\n\n".join(lines)

    # If we have LLM access, get a quick assessment
    if len(new_items) >= 2:
        headlines = "\n".join(f"- {i['title']}" for i in new_items)
        context = []
        for p in db.get_portfolios():
            context.append(portfolio.format_portfolio_text(p["id"]))

        prompt = (
            f"Portfolio:\n{''.join(context)}\n\n"
            f"Today's relevant headlines:\n{headlines}\n\n"
            "In 2-3 sentences: what's the overall theme and how might it affect this portfolio?"
        )
        try:
            summary = llm.chat([{"role": "user", "content": prompt}], max_tokens=300)
            message += f"\n\n💡 {summary}"
        except Exception:
            pass

    db.insert_agent_log("news_alert", message[:200], message)
    return message


# ─── Daily analysis (morning brief) ──────────────────────────────

def run_daily_analysis():
    """Morning market brief — sent once at 07:30."""
    context_parts = []
    for p in db.get_portfolios():
        context_parts.append(portfolio.format_portfolio_text(p["id"]))
        m = db.get_latest_risk_metrics(p["id"])
        if m:
            context_parts.append(
                f"Risk: Sharpe={m['sharpe_ratio']:.2f}, "
                f"Vol={m['volatility_annual']:.1%}, MDD={m['max_drawdown']:.1%}"
            )

        # Include value trend from dashboard snapshots
        value_hist = db.get_portfolio_total_value_history(p["id"], days=7)
        if len(value_hist) >= 2:
            latest = value_hist[0]["total_value"]
            week_ago = value_hist[-1]["total_value"]
            week_change = (latest - week_ago) / week_ago if week_ago else 0
            context_parts.append(
                f"Value trend: £{latest:,.0f} ({week_change:+.1%} this week)"
            )

        # Include drift from optimizer targets if available
        opt = db.get_latest_optimization(p["id"])
        if opt:
            rows, total = portfolio.get_portfolio_summary(p["id"])
            current_weights = {r["ticker"]: r["weight"] for r in rows}
            total_drift = sum(
                abs(current_weights.get(t, 0) - w)
                for t, w in opt["weights"].items()
            )
            if total_drift > 0.15:
                context_parts.append(
                    f"⚠ Portfolio drift from optimizer target: {total_drift:.0%}"
                )

    macro = db.get_latest_macro()
    macro_lines = [f"  {k}: {v['value']:.2f}" for k, v in macro.items()]
    context_parts.append(f"Macro indicators:\n" + "\n".join(macro_lines))

    # Flag concerning macro conditions
    vix = macro.get("^VIX", {}).get("value", 0)
    oil = macro.get("CL=F", {}).get("value", 0)
    if vix > 25:
        context_parts.append(f"⚠ VIX at {vix:.1f} — elevated fear")
    if oil > 90:
        context_parts.append(f"⚠ Oil at ${oil:.0f}/bbl — supply concerns")

    # Recent trades for context
    recent_trades = db.get_transaction_log(limit=5)
    if recent_trades:
        trade_lines = [
            f"  {t['logged_at'][:10]} {t['action']} {t['ticker']} ({t['portfolio_id']})"
            for t in recent_trades
        ]
        context_parts.append(f"Recent trades:\n" + "\n".join(trade_lines))

    # Include today's news
    news = fetch_relevant_news()
    if news:
        headlines = "\n".join(f"  - {n['title']}" for n in news[:5])
        context_parts.append(f"Today's relevant news:\n{headlines}")

    prompt = (
        "\n\n".join(context_parts) + "\n\n"
        "Provide a concise morning brief:\n"
        "1. Key overnight moves affecting the portfolio\n"
        "2. Portfolio value trend and any drift from optimizer targets\n"
        "3. What to watch today\n"
        "4. Any macro/geopolitical concerns (Middle East, trade policy, rates)\n"
        "5. One actionable suggestion if warranted\n\n"
        "Keep it under 200 words — this is a Telegram message."
    )

    analysis = llm.chat([{"role": "user", "content": prompt}], max_tokens=1000)
    db.insert_agent_log("daily_analysis", analysis[:200], analysis)
    logger.info("Daily analysis complete")
    return f"☀️ Morning Brief\n\n{analysis}"


# ─── Weekly review ────────────────────────────────────────────────

def run_weekly_review():
    """Weekend portfolio review with rebalancing suggestions.

    Includes NVIDIA optimizer results, risk trends, trade history, and macro context.
    """
    context_parts = []
    for p in db.get_portfolios():
        rows, total = portfolio.get_portfolio_summary(p["id"])
        context_parts.append(f"{p['name']} (£{total:,.0f}):")
        for r in rows:
            context_parts.append(f"  {r['ticker']:12s} {r['weight']:>5.1%}  £{r['market_value']:>10,.0f}")
        m = db.get_latest_risk_metrics(p["id"])
        if m:
            context_parts.append(
                f"  Risk: Sharpe={m['sharpe_ratio']:.2f}, Vol={m['volatility_annual']:.1%}, "
                f"MDD={m['max_drawdown']:.1%}, CVaR={m['cvar_95']:.2%}"
            )

        # Risk trend from dashboard snapshots
        risk_hist = db.get_risk_history(p["id"], days=30)
        if len(risk_hist) >= 7:
            recent_sharpe = risk_hist[0].get("sharpe_ratio")
            older_sharpe = risk_hist[-1].get("sharpe_ratio")
            if recent_sharpe is not None and older_sharpe is not None:
                sharpe_delta = recent_sharpe - older_sharpe
                context_parts.append(f"  Sharpe trend (30d): {sharpe_delta:+.2f}")

        # Value trend
        value_hist = db.get_portfolio_total_value_history(p["id"], days=30)
        if len(value_hist) >= 2:
            latest_val = value_hist[0]["total_value"]
            month_ago_val = value_hist[-1]["total_value"]
            month_change = (latest_val - month_ago_val) / month_ago_val if month_ago_val else 0
            context_parts.append(f"  Value trend (30d): {month_change:+.1%}")

        # Include optimizer recommendation if available
        opt = db.get_latest_optimization(p["id"])
        if opt:
            context_parts.append(f"\n  NVIDIA optimizer recommendation ({opt['run_date']}, {opt['run_type']}):")
            context_parts.append(f"  CVaR: {opt.get('cvar', 0):.4f}  |  E[return]: {opt.get('expected_return', 0):.4f}")
            current_weights = {r["ticker"]: r["weight"] for r in rows}
            for ticker, w in sorted(opt["weights"].items(), key=lambda x: -x[1]):
                cw = current_weights.get(ticker, 0)
                drift = cw - w
                drift_str = f" (drift: {drift:+.1%})" if abs(drift) > 0.02 else ""
                context_parts.append(f"    {ticker:12s} target: {w:.1%}{drift_str}")

    # Recent trades across all portfolios
    recent_trades = db.get_transaction_log(limit=10)
    if recent_trades:
        context_parts.append("\nRecent trades this week:")
        for t in recent_trades:
            context_parts.append(
                f"  {t['logged_at'][:10]} {t['action']} {t['ticker']} "
                f"in {t['portfolio_id']} ({t.get('shares_before', 0):.1f} → {t.get('shares_after', 0):.1f})"
            )

    # Macro context
    macro = db.get_latest_macro()
    if macro:
        macro_lines = []
        labels = {"^VIX": "VIX", "GC=F": "Gold", "CL=F": "Oil", "^TNX": "10Y", "GBPUSD=X": "GBP/USD"}
        for ind, data in macro.items():
            label = labels.get(ind, ind)
            macro_lines.append(f"  {label}: {data['value']:.2f}")
        context_parts.append(f"\nMacro:\n" + "\n".join(macro_lines))

    prompt = (
        "\n".join(context_parts) + "\n\n"
        "Weekly review:\n"
        "1. Concentration risk — flag any position over 20%\n"
        "2. Sector balance — UK banks, defence, consumer, tech, ETFs\n"
        "3. Geographic split — UK vs US vs other\n"
        "4. Risk trend — is Sharpe/volatility improving or deteriorating?\n"
        "5. Portfolio value trend — any concerning patterns?\n"
        "6. Top 3 specific rebalancing actions with reasoning\n"
        "7. If NVIDIA optimizer recommendations are shown, compare current weights to targets "
        "and identify the biggest drift gaps. Prefix with 'NVIDIA optimizer recommends...'\n"
        "8. Macro outlook for next week — flag any conditions that warrant stress testing\n\n"
        "Be specific — name positions and amounts. Note any GIA (IBKR) positions if present."
    )

    analysis = llm.chat([{"role": "user", "content": prompt}], max_tokens=2000)
    db.insert_agent_log("weekly_review", analysis[:200], analysis)
    logger.info("Weekly review complete")
    return f"📋 Weekly Review\n\n{analysis}"


# ─── Weekly optimisation (scheduled) ────────────────────────────────

def run_weekly_optimize():
    """Run NVIDIA cufolio optimizer for all portfolios and store results.

    Returns a Telegram message summarising the optimal weights.
    """
    from jezclaw import spark_client

    messages = []
    for p in db.get_portfolios():
        positions = db.get_positions(p["id"])
        if not positions:
            continue

        tickers = [pos["ticker"] for pos in positions]
        rows, total_val = portfolio.get_portfolio_summary(p["id"])
        current_weights = {r["ticker"]: r["weight"] for r in rows}

        try:
            result = spark_client.optimize(tickers=tickers, w_max=0.25)
        except Exception as e:
            logger.error(f"Weekly optimize failed for {p['id']}: {e}")
            continue

        opt_weights = result["weights"]
        metrics = result["metrics"]

        # Store result
        db.insert_optimization_result(
            p["id"], "scheduled", opt_weights,
            result.get("cash", 0), metrics["expected_return"],
            metrics["cvar"], result.get("solver_used", ""),
        )

        # Build message
        lines = [f"\n{p['name']} — NVIDIA Optimizer"]
        lines.append(f"CVaR: {metrics['cvar']:.4f}  |  E[return]: {metrics['expected_return']:.4f}")

        # Show biggest delta trades
        deltas = []
        for t in set(list(current_weights) + list(opt_weights)):
            cw = current_weights.get(t, 0)
            ow = opt_weights.get(t, 0)
            d = ow - cw
            if abs(d) > 0.02:  # >2% change
                action = "BUY" if d > 0 else "SELL"
                deltas.append((t, d, action))

        deltas.sort(key=lambda x: abs(x[1]), reverse=True)
        if deltas:
            lines.append("Rebalancing suggestions:")
            for t, d, a in deltas[:5]:
                lines.append(f"  {a} {t}: {d:+.1%}")

        messages.append("\n".join(lines))

    if not messages:
        return None

    msg = "🎯 Weekly Optimization\n" + "\n".join(messages)
    db.insert_agent_log("weekly_optimize", msg[:200], msg)
    logger.info("Weekly optimization complete")
    return msg


# ─── Daily snapshot (silent) ──────────────────────────────────────

def run_daily_snapshot():
    """Take daily snapshot of all portfolios. No notification."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    for p in db.get_portfolios():
        portfolio.calculate_risk_metrics(p["id"])
        db.take_position_snapshot(p["id"], today)
    logger.info(f"Daily snapshot taken for {today}")
