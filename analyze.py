"""
Claude Trading Bot — Autonomous Market Analyzer
Fetches market data, sends to Claude for analysis, notifies Discord on signals.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone


DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


def fetch_json(url: str) -> dict:
    """Fetch JSON from a URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "ClaudeTradingBot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def get_crypto_data() -> dict:
    """Fetch crypto prices from CoinGecko (free, no key needed)."""
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin,ethereum,solana"
        "&vs_currencies=eur"
        "&include_24hr_change=true"
        "&include_market_cap=true"
    )
    return fetch_json(url)


def get_fear_greed_index() -> dict:
    """Fetch crypto fear & greed index."""
    url = "https://api.alternative.me/fng/?limit=1"
    return fetch_json(url)


def get_stock_market_summary() -> str:
    """Fetch major index data from Yahoo Finance API (unofficial)."""
    indices = {
        "S&P 500": "^GSPC",
        "Nasdaq": "^IXIC",
        "DAX": "^GDAXI",
        "EURO STOXX 50": "^STOXX50E",
    }
    results = []
    for name, symbol in indices.items():
        try:
            url = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                f"?interval=1d&range=5d"
            )
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0"
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            meta = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice", 0)
            prev_close = meta.get("chartPreviousClose", 0)
            change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
            results.append(f"{name}: {price:.2f} ({change_pct:+.2f}%)")
        except Exception as e:
            results.append(f"{name}: unavailable ({e})")
    return "\n".join(results)


def read_file_from_repo(filename: str) -> str:
    """Read a file from the repo (works both locally and in GitHub Actions)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"({filename} not found)"


def call_claude(market_data: str, portfolio: str, trades: str, strategy: str) -> str:
    """Send market data to Claude for analysis."""
    prompt = f"""You are the autonomous trading analyst for a 300€ portfolio on Trade Republic.

## Your Strategy Rules
{strategy}

## Current Portfolio
{portfolio}

## Trade History
{trades}

## Current Market Data (as of {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})
{market_data}

## Your Task
Analyze the market data against the strategy rules. You must respond in one of two ways:

**If you see a trade opportunity** (meets 2:1 reward:risk, clear catalyst, fits position sizing):
Respond with SIGNAL: followed by:
- ACTION: BUY or SELL
- ASSET: name and ticker/ISIN if known
- AMOUNT: euros to deploy
- ENTRY: target price or "market"
- STOP-LOSS: price level
- TAKE-PROFIT: price level(s)
- TIMEFRAME: expected hold duration
- THESIS: 1-2 sentence reasoning
- CONFIDENCE: Low / Medium / High

**If no clear opportunity exists:**
Respond with NO_SIGNAL: followed by a brief 1-2 sentence market summary.

**If markets are crashing or something extraordinary is happening:**
Respond with ALERT: followed by what's happening and whether to take any defensive action.

Be disciplined. Only signal when conviction is genuine. This is real money."""

    body = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode())

    return result["content"][0]["text"]


def send_discord(message: str):
    """Send a message to Discord via webhook."""
    body = json.dumps({"content": message[:2000]}).encode()
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        pass  # 204 No Content on success


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting market analysis...")

    # 1. Gather market data
    print("Fetching crypto data...")
    try:
        crypto = get_crypto_data()
        crypto_str = "\n".join(
            f"  {name}: €{data.get('eur', 'N/A')} (24h: {data.get('eur_24h_change', 0):+.2f}%)"
            for name, data in crypto.items()
        )
    except Exception as e:
        crypto_str = f"  Crypto data unavailable: {e}"

    print("Fetching fear & greed index...")
    try:
        fng = get_fear_greed_index()
        fng_data = fng["data"][0]
        fng_str = f"  Fear & Greed Index: {fng_data['value']} ({fng_data['value_classification']})"
    except Exception as e:
        fng_str = f"  Fear & Greed: unavailable ({e})"

    print("Fetching stock indices...")
    stock_str = get_stock_market_summary()

    market_data = f"""CRYPTO:
{crypto_str}
{fng_str}

INDICES:
{stock_str}"""

    print("Market data collected:")
    print(market_data)

    # 2. Read portfolio state and strategy
    portfolio = read_file_from_repo("portfolio.md")
    trades = read_file_from_repo("trades.md")
    strategy = read_file_from_repo("CLAUDE.md")

    # 3. Analyze with Claude
    print("\nSending to Claude for analysis...")
    analysis = call_claude(market_data, portfolio, trades, strategy)
    print(f"\nClaude's analysis:\n{analysis}")

    # 4. Notify if needed
    if analysis.startswith("SIGNAL:"):
        message = f"**TRADE SIGNAL**\n{analysis[7:].strip()}"
        send_discord(message)
        print("Discord notification sent (SIGNAL)")
    elif analysis.startswith("ALERT:"):
        message = f"**MARKET ALERT**\n{analysis[6:].strip()}"
        send_discord(message)
        print("Discord notification sent (ALERT)")
    elif analysis.startswith("NO_SIGNAL:"):
        print("No signal — no notification sent.")
    else:
        # Fallback: if Claude's response doesn't match expected format,
        # check if it contains signal-like content
        lower = analysis.lower()
        if "buy" in lower or "sell" in lower or "alert" in lower:
            message = f"**ANALYSIS UPDATE**\n{analysis[:1900]}"
            send_discord(message)
            print("Discord notification sent (fallback)")
        else:
            print("No actionable signal — no notification sent.")

    print("\nDone.")


if __name__ == "__main__":
    main()
