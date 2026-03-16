"""
Claude Trading Bot — Autonomous Market Analyzer
Two-stage analysis:
  1. Fetch broad market data + news → Claude picks specific assets to research
  2. Fetch detailed data on those assets → Claude makes final trade decision
"""

import json
import os
import re
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


# ---------------------------------------------------------------------------
# Data fetching helpers
# ---------------------------------------------------------------------------

def fetch_json(url: str, headers: dict | None = None) -> dict:
    """Fetch JSON from a URL."""
    hdrs = {"User-Agent": "ClaudeTradingBot/1.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def fetch_text(url: str) -> str:
    """Fetch raw text/XML from a URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode()


def get_crypto_data() -> str:
    """Fetch crypto prices from CoinGecko."""
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin,ethereum,solana,ripple,cardano,avalanche-2,chainlink,polkadot"
        "&vs_currencies=eur"
        "&include_24hr_change=true"
    )
    data = fetch_json(url)
    lines = []
    for name, vals in data.items():
        price = vals.get("eur", "N/A")
        change = vals.get("eur_24h_change", 0)
        lines.append(f"  {name}: €{price} (24h: {change:+.2f}%)")
    return "\n".join(sorted(lines))


def get_crypto_trending() -> str:
    """Fetch trending coins from CoinGecko."""
    try:
        data = fetch_json("https://api.coingecko.com/api/v3/search/trending")
        coins = data.get("coins", [])[:7]
        lines = []
        for c in coins:
            item = c["item"]
            price_change = item.get("data", {}).get("price_change_percentage_24h", {}).get("eur", 0)
            lines.append(f"  {item['name']} ({item['symbol']}): 24h: {price_change:+.2f}%")
        return "\n".join(lines) if lines else "  No trending data"
    except Exception as e:
        return f"  Trending unavailable: {e}"


def get_fear_greed_index() -> str:
    """Fetch crypto fear & greed index."""
    try:
        data = fetch_json("https://api.alternative.me/fng/?limit=1")
        fng = data["data"][0]
        return f"  Fear & Greed Index: {fng['value']} ({fng['value_classification']})"
    except Exception as e:
        return f"  Fear & Greed: unavailable ({e})"


def get_index_data() -> str:
    """Fetch major index data from Yahoo Finance."""
    indices = {
        "S&P 500": "^GSPC",
        "Nasdaq": "^IXIC",
        "DAX": "^GDAXI",
        "EURO STOXX 50": "^STOXX50E",
        "FTSE 100": "^FTSE",
        "Nikkei 225": "^N225",
    }
    results = []
    for name, symbol in indices.items():
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            meta = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice", 0)
            prev_close = meta.get("chartPreviousClose", 0)
            change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
            results.append(f"  {name}: {price:.2f} ({change_pct:+.2f}%)")
        except Exception as e:
            results.append(f"  {name}: unavailable ({e})")
    return "\n".join(results)


def get_market_news() -> str:
    """Fetch market news from Google News RSS feeds."""
    feeds = [
        ("Business", "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB"),
        ("Markets", "https://news.google.com/rss/search?q=stock+market+today&hl=en-US&gl=US&ceid=US:en"),
        ("Crypto", "https://news.google.com/rss/search?q=cryptocurrency+market&hl=en-US&gl=US&ceid=US:en"),
    ]
    all_headlines = []
    for category, url in feeds:
        try:
            xml_text = fetch_text(url)
            root = ET.fromstring(xml_text)
            items = root.findall(".//item")[:5]
            for item in items:
                title = item.find("title")
                pub_date = item.find("pubDate")
                if title is not None and title.text:
                    date_str = ""
                    if pub_date is not None and pub_date.text:
                        date_str = f" [{pub_date.text[:16]}]"
                    all_headlines.append(f"  [{category}] {title.text}{date_str}")
        except Exception as e:
            all_headlines.append(f"  [{category}] News unavailable: {e}")
    return "\n".join(all_headlines[:20])


def get_top_movers() -> str:
    """Fetch top gaining/losing stocks from Yahoo Finance."""
    results = []
    for label, url_part in [("Top Gainers", "gainers"), ("Top Losers", "losers")]:
        try:
            url = f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds={url_part}&count=5"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            quotes = data.get("finance", {}).get("result", [{}])[0].get("quotes", [])
            results.append(f"  {label}:")
            for q in quotes[:5]:
                name = q.get("shortName", q.get("symbol", "?"))
                symbol = q.get("symbol", "?")
                change = q.get("regularMarketChangePercent", 0)
                price = q.get("regularMarketPrice", 0)
                results.append(f"    {name} ({symbol}): ${price:.2f} ({change:+.2f}%)")
        except Exception as e:
            results.append(f"  {label}: unavailable ({e})")
    return "\n".join(results)


def get_stock_detail(symbol: str) -> dict | None:
    """Fetch detailed data for a specific stock/ETF."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1mo"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        result = data["chart"]["result"][0]
        meta = result["meta"]
        closes = result["indicators"]["quote"][0].get("close", [])
        closes = [c for c in closes if c is not None]

        price = meta.get("regularMarketPrice", 0)
        prev_close = meta.get("chartPreviousClose", 0)
        day_change = ((price - prev_close) / prev_close * 100) if prev_close else 0

        month_start = closes[0] if closes else price
        month_change = ((price - month_start) / month_start * 100) if month_start else 0

        high_1m = max(closes) if closes else price
        low_1m = min(closes) if closes else price

        return {
            "symbol": symbol,
            "price": price,
            "currency": meta.get("currency", "USD"),
            "day_change_pct": day_change,
            "month_change_pct": month_change,
            "high_1m": high_1m,
            "low_1m": low_1m,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

def call_claude(prompt: str, max_tokens: int = 1024) -> str:
    """Call Claude API with a prompt."""
    body = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
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
    # Discord 2000 char limit — split if needed
    chunks = [message[i:i+1990] for i in range(0, len(message), 1990)]
    for chunk in chunks:
        body = json.dumps({"content": chunk}).encode()
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass


def read_file_from_repo(filename: str) -> str:
    """Read a file from the repo."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"({filename} not found)"


# ---------------------------------------------------------------------------
# Main two-stage analysis
# ---------------------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)
    print(f"[{now.isoformat()}] Starting market analysis...")

    # ---- Stage 1: Gather broad market data ----
    print("\n=== STAGE 1: Gathering broad market data ===")

    print("Fetching crypto prices...")
    crypto_str = get_crypto_data()

    print("Fetching crypto trending...")
    trending_str = get_crypto_trending()

    print("Fetching fear & greed index...")
    fng_str = get_fear_greed_index()

    print("Fetching market indices...")
    index_str = get_index_data()

    print("Fetching market news...")
    news_str = get_market_news()

    print("Fetching top movers...")
    movers_str = get_top_movers()

    broad_data = f"""CRYPTO PRICES:
{crypto_str}

TRENDING CRYPTO:
{trending_str}

SENTIMENT:
{fng_str}

MAJOR INDICES:
{index_str}

TOP STOCK MOVERS:
{movers_str}

MARKET NEWS:
{news_str}"""

    print("\nBroad market data collected:")
    print(broad_data)

    # Read portfolio state
    portfolio = read_file_from_repo("portfolio.md")
    trades = read_file_from_repo("trades.md")
    strategy = read_file_from_repo("CLAUDE.md")

    # ---- Stage 1 Claude call: identify specific assets to research ----
    print("\n=== STAGE 1: Asking Claude to identify assets to research ===")

    stage1_prompt = f"""You are the autonomous trading analyst for a 300€ portfolio on Trade Republic (German broker).

## Current Market Data ({now.strftime('%Y-%m-%d %H:%M UTC')})
{broad_data}

## Current Portfolio
{portfolio}

## Your Strategy (summary)
- 300€ capital, moderate risk, Trade Republic platform
- Max 5% loss per trade, 2:1 min reward:risk
- Max 3 positions, no position > 40% of portfolio
- Core (60-70%), Tactical (20-30%), Cash reserve (10-20%)

Based on the news, movers, and market conditions above, identify 3-6 specific assets (stocks, ETFs, or crypto) that deserve deeper analysis right now. For each, explain WHY in one sentence.

Respond in this exact format (one per line):
RESEARCH: <TICKER_SYMBOL> | <reason>

Examples:
RESEARCH: NVDA | AI spending news could drive momentum, already a top mover
RESEARCH: BTC-EUR | Extreme fear with positive 24h reversal, potential bounce play

Only suggest assets available on Trade Republic (EU/US stocks, major ETFs, major crypto)."""

    stage1_result = call_claude(stage1_prompt, max_tokens=512)
    print(f"\nStage 1 result:\n{stage1_result}")

    # Parse tickers from stage 1
    tickers = re.findall(r"RESEARCH:\s*([A-Z0-9\.\-]+)\s*\|", stage1_result)
    print(f"\nTickers to research: {tickers}")

    # ---- Stage 2: Fetch detailed data on specific assets ----
    print("\n=== STAGE 2: Fetching detailed asset data ===")

    asset_details = []
    for ticker in tickers[:6]:
        # Skip crypto tickers (already have data from CoinGecko)
        if ticker in ("BTC-EUR", "ETH-EUR", "SOL-EUR", "BTC", "ETH", "SOL"):
            asset_details.append(f"  {ticker}: See crypto data above (already fetched)")
            continue

        print(f"  Fetching {ticker}...")
        detail = get_stock_detail(ticker)
        if detail:
            asset_details.append(
                f"  {detail['symbol']}: {detail['currency']} {detail['price']:.2f} "
                f"(day: {detail['day_change_pct']:+.2f}%, month: {detail['month_change_pct']:+.2f}%, "
                f"1m range: {detail['low_1m']:.2f}-{detail['high_1m']:.2f})"
            )
        else:
            asset_details.append(f"  {ticker}: data unavailable")

    detail_str = "\n".join(asset_details)
    print(f"\nAsset details:\n{detail_str}")

    # ---- Stage 2 Claude call: final analysis and trade decision ----
    print("\n=== STAGE 2: Final analysis ===")

    stage2_prompt = f"""You are the autonomous trading analyst for a 300€ portfolio on Trade Republic.

## Your Full Strategy
{strategy}

## Current Portfolio
{portfolio}

## Trade History
{trades}

## Broad Market Context ({now.strftime('%Y-%m-%d %H:%M UTC')})
{broad_data}

## Detailed Asset Research
Assets identified for deeper analysis:
{stage1_result}

Detailed price data:
{detail_str}

## Your Task
Based on ALL the data above — news, market conditions, specific asset data, and your strategy rules — make your final decision.

Respond in EXACTLY one of these formats:

**If you see a trade opportunity** (meets 2:1 reward:risk, clear catalyst, fits position sizing):
SIGNAL:
- ACTION: BUY or SELL
- ASSET: full name, ticker, and ISIN if known
- AMOUNT: euros to deploy (respect position sizing rules)
- ENTRY: target price or "market"
- STOP-LOSS: specific price level with % from entry
- TAKE-PROFIT: specific price level(s) with % from entry
- TIMEFRAME: expected hold duration
- THESIS: 2-3 sentence reasoning citing specific news/data
- CONFIDENCE: Low / Medium / High

You may include up to 2 signals if multiple opportunities exist.

**If no clear opportunity exists:**
NO_SIGNAL: 2-3 sentence summary of market state and what you're watching for.

**If markets are crashing or something extraordinary is happening:**
ALERT: what's happening and recommended defensive action.

Be specific. Cite the news and data that drive your reasoning. This is real money."""

    analysis = call_claude(stage2_prompt, max_tokens=1500)
    print(f"\nFinal analysis:\n{analysis}")

    # ---- Notify ----
    # Claude sometimes adds preamble before the keyword — search anywhere in text
    analysis_clean = analysis.strip()
    has_signal = "SIGNAL:" in analysis_clean and "NO_SIGNAL:" not in analysis_clean
    has_alert = "ALERT:" in analysis_clean
    has_no_signal = "NO_SIGNAL:" in analysis_clean

    try:
        if has_signal:
            # Extract everything from SIGNAL: onward
            signal_start = analysis_clean.index("SIGNAL:")
            signal_text = analysis_clean[signal_start + 7:].strip()
            message = f"**TRADE SIGNAL** ({now.strftime('%Y-%m-%d %H:%M UTC')})\n\n{signal_text}"
            send_discord(message)
            print("\nDiscord notification sent (SIGNAL)")
        elif has_alert:
            alert_start = analysis_clean.index("ALERT:")
            alert_text = analysis_clean[alert_start + 6:].strip()
            message = f"**MARKET ALERT** ({now.strftime('%Y-%m-%d %H:%M UTC')})\n\n{alert_text}"
            send_discord(message)
            print("\nDiscord notification sent (ALERT)")
        elif has_no_signal:
            print("\nNo signal — no notification sent.")
        else:
            print("\nUnrecognized format — no notification sent.")
    except Exception as e:
        print(f"\nError sending Discord notification: {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
