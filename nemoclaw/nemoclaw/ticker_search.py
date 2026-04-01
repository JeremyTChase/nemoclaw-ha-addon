"""Yahoo Finance ticker search."""

import logging
import requests

logger = logging.getLogger("nemoclaw.ticker_search")


def search_tickers(query, max_results=8):
    if not query or len(query) < 2:
        return []
    try:
        url = "https://query2.finance.yahoo.com/v1/finance/search"
        r = requests.get(
            url,
            params={"q": query, "quotesCount": max_results, "newsCount": 0},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        r.raise_for_status()
        return [
            {"symbol": q.get("symbol", ""), "name": q.get("shortname", ""), "exchange": q.get("exchange", "")}
            for q in r.json().get("quotes", [])
        ]
    except Exception as e:
        logger.warning(f"Search failed: {e}")
        return []
