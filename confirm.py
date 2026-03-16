"""
Claude Trading Bot — Trade Confirmation Script
Triggered manually by the user when they're ready to execute a signal.
Fetches fresh market data, compares to original signal conditions,
and confirms or retracts the trade recommendation.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

# Re-use data fetchers from analyze.py
from analyze import (
    get_crypto_data,
    get_crypto_trending,
    get_fear_greed_index,
    get_index_data,
    get_market_news,
    get_stock_detail,
    call_claude,
    send_discord,
    read_file_from_repo,
)


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_last_signal() -> dict | None:
    """Load the most recent signal snapshot."""
    path = os.path.join(SCRIPT_DIR, "last_signal.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def main():
    now = datetime.now(timezone.utc)
    print(f"[{now.isoformat()}] Trade confirmation check...\n")

    # 1. Load original signal
    snapshot = load_last_signal()
    if not snapshot:
        print("ERROR: No signal found. Run analyze.py first or check last_signal.json.")
        send_discord("**CONFIRM ERROR**: No pending signal found to confirm.")
        sys.exit(1)

    signal_time = snapshot["timestamp"]
    original_signal = snapshot["signal"]
    original_market_data = snapshot["market_data"]
    original_asset_details = snapshot["asset_details"]

    print(f"Original signal from: {signal_time}")
    print(f"Signal:\n{original_signal}\n")

    # 2. Fetch fresh market data
    print("=== Fetching current market data ===")

    print("Fetching crypto prices...")
    crypto_str = get_crypto_data()

    print("Fetching fear & greed index...")
    fng_str = get_fear_greed_index()

    print("Fetching market indices...")
    index_str = get_index_data()

    print("Fetching market news...")
    news_str = get_market_news()

    current_data = f"""CRYPTO PRICES:
{crypto_str}

SENTIMENT:
{fng_str}

MAJOR INDICES:
{index_str}

LATEST NEWS:
{news_str}"""

    print(f"\nCurrent market data:\n{current_data}")

    # 3. Fetch fresh detail on the specific asset(s) from the signal
    # Try to extract tickers from the signal text
    import re
    # Look for common ticker patterns in the signal
    potential_tickers = re.findall(r'\(([A-Z]{2,5}(?:-EUR)?)\)', original_signal)
    # Also check for crypto mentions
    crypto_map = {
        "bitcoin": "BTC-EUR", "btc": "BTC-EUR",
        "ethereum": "ETH-EUR", "eth": "ETH-EUR",
        "solana": "SOL-EUR", "sol": "SOL-EUR",
    }
    signal_lower = original_signal.lower()
    for name, ticker in crypto_map.items():
        if name in signal_lower and ticker not in potential_tickers:
            potential_tickers.append(ticker)

    fresh_details = []
    for ticker in potential_tickers[:4]:
        if ticker in ("BTC-EUR", "ETH-EUR", "SOL-EUR"):
            fresh_details.append(f"  {ticker}: See current crypto prices above")
        else:
            detail = get_stock_detail(ticker)
            if detail:
                fresh_details.append(
                    f"  {detail['symbol']}: {detail['currency']} {detail['price']:.2f} "
                    f"(day: {detail['day_change_pct']:+.2f}%, month: {detail['month_change_pct']:+.2f}%, "
                    f"1m range: {detail['low_1m']:.2f}-{detail['high_1m']:.2f})"
                )
    fresh_detail_str = "\n".join(fresh_details) if fresh_details else "  (no specific asset data fetched)"

    # 4. Read current portfolio/strategy
    portfolio = read_file_from_repo("portfolio.md")
    strategy = read_file_from_repo("CLAUDE.md")

    # 5. Ask Claude to compare and confirm
    print("\n=== Asking Claude to confirm or retract ===")

    hours_elapsed = "unknown"
    try:
        from datetime import datetime as dt
        original_dt = dt.fromisoformat(signal_time)
        delta = now - original_dt
        hours_elapsed = f"{delta.total_seconds() / 3600:.1f} hours"
    except Exception:
        pass

    prompt = f"""You are the trading analyst for a 300€ portfolio on Trade Republic.

You issued a trade signal {hours_elapsed} ago. The user is now ready to execute.
Your job: compare CURRENT market conditions to what they were when you issued the signal, and decide whether to CONFIRM, MODIFY, or RETRACT.

## Your Strategy
{strategy}

## Current Portfolio
{portfolio}

## ORIGINAL SIGNAL (issued {signal_time})
{original_signal}

## MARKET DATA AT TIME OF SIGNAL
{original_market_data}

## ASSET DETAILS AT TIME OF SIGNAL
{original_asset_details}

## CURRENT MARKET DATA (now: {now.strftime('%Y-%m-%d %H:%M UTC')})
{current_data}

## CURRENT ASSET DETAILS
{fresh_detail_str}

## Your Task
Compare the two snapshots carefully. Consider:
1. Has the asset price moved significantly since the signal? Is the entry still attractive?
2. Has the thesis changed? Any new news that supports or undermines it?
3. Has market sentiment shifted?
4. Does the reward:risk ratio still hold at current prices?
5. Have stop-loss or take-profit levels been breached already?

Respond in EXACTLY one of these formats:

**CONFIRM**: The signal still stands. Include any adjustments to entry price.
CONFIRM:
- ORIGINAL SIGNAL: (brief summary)
- CURRENT PRICE: (current price vs original entry)
- PRICE CHANGE SINCE SIGNAL: (% and direction)
- THESIS STATUS: (still valid / strengthened / weakened but holding)
- ADJUSTED ENTRY: (new entry price if different, or "unchanged")
- ADJUSTED STOP-LOSS: (if needed)
- ADJUSTED TAKE-PROFIT: (if needed)
- VERDICT: Execute the trade. (1 sentence on why it's still good)

**MODIFY**: The core thesis holds but parameters need significant changes.
MODIFY:
- ORIGINAL SIGNAL: (brief summary)
- WHAT CHANGED: (specific changes in market/price)
- NEW SIGNAL: (full updated signal with all parameters)
- VERDICT: (1 sentence on why the modification is needed)

**RETRACT**: Conditions have changed enough that the trade no longer makes sense.
RETRACT:
- ORIGINAL SIGNAL: (brief summary)
- WHAT CHANGED: (specific reasons)
- VERDICT: Do not execute. (1 sentence on why)

Be honest. If the opportunity has passed, say so. Protecting capital is more important than being consistent."""

    result = call_claude(prompt, max_tokens=1024)
    print(f"\nConfirmation result:\n{result}")

    # 6. Send to Discord
    result_clean = result.strip()
    try:
        if "CONFIRM:" in result_clean:
            emoji = "CONFIRMED"
            color_note = "Signal confirmed"
        elif "MODIFY:" in result_clean:
            emoji = "MODIFIED"
            color_note = "Signal modified"
        elif "RETRACT:" in result_clean:
            emoji = "RETRACTED"
            color_note = "Signal retracted"
        else:
            emoji = "UPDATE"
            color_note = "Confirmation update"

        message = (
            f"**TRADE {emoji}** ({now.strftime('%Y-%m-%d %H:%M UTC')})\n"
            f"Original signal from: {signal_time}\n\n"
            f"{result_clean[:1800]}"
        )
        send_discord(message)
        print(f"\nDiscord notification sent ({color_note})")
    except Exception as e:
        print(f"\nError sending Discord notification: {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
