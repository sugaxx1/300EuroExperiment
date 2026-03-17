"""
Volatile Asset Tracker — High-Frequency Trading Monitor
Runs every 10 minutes via GitHub Actions. Three-tier architecture:
  1. Python pre-filter (free) — checks if prices moved enough
  2. Haiku triage (~$0.003) — quick assessment
  3. Sonnet decision (~$0.04) — full entry/exit analysis

State persisted in volatile_state.json (git-committed between runs).
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

from analyze import (
    fetch_json,
    get_crypto_data,
    get_fear_greed_index,
    get_stock_detail,
    get_market_news,
    send_discord,
    read_file_from_repo,
    ANTHROPIC_API_KEY,
    ANTHROPIC_API_URL,
)

import urllib.request

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "volatile_state.json")

# Pre-filter thresholds
WATCH_MOVE_PCT = 2.0        # % move since last check to trigger Haiku (WATCHING)
WATCH_HOUR_PCT = 5.0        # % move over last hour to trigger Haiku (WATCHING)
POS_MOVE_PCT = 1.5          # % move since last check to trigger Haiku (IN_POSITION)
POS_HOUR_PCT = 3.0          # % move over last hour to trigger Haiku (IN_POSITION)
HAIKU_RECHECK_WATCHING = 2  # hours between periodic Haiku calls (WATCHING)
HAIKU_RECHECK_POSITION = 1  # hours between periodic Haiku calls (IN_POSITION)
TRAILING_STOP_PCT = 2.0     # trailing stop: exit if price drops this % from high
COOLDOWN_HOURS = 1          # hours to wait after exiting before re-entering

# Daily API caps
MAX_HAIKU_PER_DAY = 50
MAX_SONNET_PER_DAY = 10

# Price history: keep last 6 data points (1 hour at 10-min intervals)
MAX_PRICE_HISTORY = 6


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Load tracker state from JSON file."""
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict):
    """Save tracker state to JSON file."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# API call tracking
# ---------------------------------------------------------------------------

def get_daily_calls(state: dict, model: str) -> int:
    """Get number of API calls made today for a model."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return state.get("daily_api_calls", {}).get(today, {}).get(model, 0)


def increment_daily_calls(state: dict, model: str):
    """Increment daily API call counter."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if "daily_api_calls" not in state:
        state["daily_api_calls"] = {}
    if today not in state["daily_api_calls"]:
        # Clean old dates, keep only today
        state["daily_api_calls"] = {today: {"haiku": 0, "sonnet": 0}}
    state["daily_api_calls"][today][model] = (
        state["daily_api_calls"][today].get(model, 0) + 1
    )


def can_call(state: dict, model: str) -> bool:
    """Check if we're under the daily API cap."""
    limit = MAX_HAIKU_PER_DAY if model == "haiku" else MAX_SONNET_PER_DAY
    return get_daily_calls(state, model) < limit


# ---------------------------------------------------------------------------
# Claude API calls
# ---------------------------------------------------------------------------

def call_haiku(prompt: str, max_tokens: int = 512) -> str:
    """Call Claude Haiku for fast, cheap triage."""
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
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

    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())

    return result["content"][0]["text"]


def call_sonnet(prompt: str, max_tokens: int = 1024) -> str:
    """Call Claude Sonnet for full analysis."""
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


# ---------------------------------------------------------------------------
# Price fetching
# ---------------------------------------------------------------------------

def fetch_watchlist_prices(watchlist: list) -> dict:
    """Fetch current prices for all watchlist assets. Returns {symbol: price_eur}."""
    prices = {}

    # Collect CoinGecko symbols
    cg_symbols = [w["symbol"] for w in watchlist if w["source"] == "coingecko"]
    if cg_symbols:
        ids = ",".join(cg_symbols)
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=eur"
        try:
            data = fetch_json(url)
            for sym in cg_symbols:
                if sym in data and "eur" in data[sym]:
                    prices[sym] = data[sym]["eur"]
        except Exception as e:
            print(f"  CoinGecko fetch error: {e}")

    # Collect Yahoo symbols (stocks)
    for w in watchlist:
        if w["source"] == "yahoo" and w.get("yahoo_symbol"):
            # Skip stocks outside market hours (rough check: weekday 7-22 UTC)
            now = datetime.now(timezone.utc)
            if now.weekday() >= 5:  # weekend
                continue
            if now.hour < 7 or now.hour > 21:
                continue
            detail = get_stock_detail(w["yahoo_symbol"])
            if detail:
                prices[w["symbol"]] = detail["price"]

    return prices


# ---------------------------------------------------------------------------
# Pre-filter logic (zero API cost)
# ---------------------------------------------------------------------------

def update_price_history(watchlist: list, prices: dict):
    """Update price history for each watchlist asset."""
    for w in watchlist:
        if w["symbol"] in prices:
            w["last_price"] = prices[w["symbol"]]
            w["price_history"].append(prices[w["symbol"]])
            # Keep only last N data points
            if len(w["price_history"]) > MAX_PRICE_HISTORY:
                w["price_history"] = w["price_history"][-MAX_PRICE_HISTORY:]


def pre_filter_watching(state: dict, prices: dict) -> list:
    """
    Check if any watchlist asset triggers Haiku analysis.
    Returns list of (symbol, reason) tuples that triggered.
    """
    triggers = []

    for w in state["watchlist"]:
        sym = w["symbol"]
        current = prices.get(sym)
        if current is None:
            continue

        last = w.get("last_price")
        history = w.get("price_history", [])

        # Check: moved >2% since last check
        if last and last > 0:
            move_pct = abs((current - last) / last * 100)
            if move_pct >= WATCH_MOVE_PCT:
                direction = "up" if current > last else "down"
                triggers.append((sym, f"{move_pct:.1f}% {direction} since last check"))

        # Check: moved >5% in last hour (6 data points at 10-min intervals)
        if len(history) >= 2:
            oldest = history[0]
            if oldest > 0:
                hour_move = abs((current - oldest) / oldest * 100)
                if hour_move >= WATCH_HOUR_PCT:
                    direction = "up" if current > oldest else "down"
                    triggers.append((sym, f"{hour_move:.1f}% {direction} in last hour"))

    # Check: time since last Haiku call
    last_haiku = state.get("last_haiku_call")
    if last_haiku:
        try:
            last_dt = datetime.fromisoformat(last_haiku)
            hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if hours_since >= HAIKU_RECHECK_WATCHING:
                triggers.append(("periodic", f"{hours_since:.1f}h since last Haiku check"))
        except (ValueError, TypeError):
            triggers.append(("periodic", "invalid last_haiku_call timestamp"))
    else:
        triggers.append(("periodic", "first run, no previous Haiku check"))

    return triggers


def pre_filter_in_position(state: dict, prices: dict) -> dict:
    """
    Check position status. Returns action dict:
    - {"action": "stop_loss"} — immediate exit
    - {"action": "take_profit", "level": idx} — TP hit
    - {"action": "trailing_stop"} — trailing stop triggered
    - {"action": "haiku", "reason": str} — needs Haiku assessment
    - {"action": "hold"} — no action needed
    """
    pos = state["position"]
    sym = pos["symbol"]
    current = prices.get(sym)
    if current is None:
        return {"action": "hold", "reason": "price unavailable"}

    entry = pos["entry_price"]
    stop_loss = pos["stop_loss"]
    take_profits = pos.get("take_profit", [])
    high_since = pos.get("high_since_entry", entry)
    trailing_pct = pos.get("trailing_stop_pct")

    # Update high since entry
    if current > high_since:
        pos["high_since_entry"] = current
        high_since = current

    # Hard stop-loss — immediate exit
    if current <= stop_loss:
        return {"action": "stop_loss"}

    # Take-profit hit
    for i, tp in enumerate(take_profits):
        if current >= tp:
            return {"action": "take_profit", "level": i}

    # Trailing stop (activated after first TP or manually)
    if trailing_pct and high_since > 0:
        trail_level = high_since * (1 - trailing_pct / 100)
        if current <= trail_level:
            return {"action": "trailing_stop", "trail_level": trail_level}

    # Check if price moved significantly since last check
    last_price = pos.get("last_check_price", entry)
    if last_price and last_price > 0:
        move_pct = abs((current - last_price) / last_price * 100)
        if move_pct >= POS_MOVE_PCT:
            direction = "up" if current > last_price else "down"
            return {"action": "haiku", "reason": f"{move_pct:.1f}% {direction} since last check"}

    # Periodic Haiku recheck
    last_haiku = state.get("last_haiku_call")
    if last_haiku:
        try:
            last_dt = datetime.fromisoformat(last_haiku)
            hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if hours_since >= HAIKU_RECHECK_POSITION:
                return {"action": "haiku", "reason": f"{hours_since:.1f}h since last check"}
        except (ValueError, TypeError):
            return {"action": "haiku", "reason": "invalid timestamp"}
    else:
        return {"action": "haiku", "reason": "first check since entry"}

    return {"action": "hold"}


# ---------------------------------------------------------------------------
# Haiku triage
# ---------------------------------------------------------------------------

def haiku_triage_watching(state: dict, prices: dict, triggers: list) -> str:
    """Ask Haiku if triggered assets are worth a full Sonnet analysis."""
    trigger_text = "\n".join(f"  - {sym}: {reason}" for sym, reason in triggers)

    price_text = "\n".join(
        f"  {sym}: EUR {price:.2f}" for sym, price in prices.items()
    )

    # Get portfolio cash
    portfolio = read_file_from_repo("portfolio.md")
    # Extract cash line
    cash_line = "unknown"
    for line in portfolio.split("\n"):
        if "Cash Available" in line:
            cash_line = line.strip()
            break

    fng = get_fear_greed_index()

    prompt = f"""You monitor volatile assets for a 300EUR trading portfolio.

CURRENT PRICES:
{price_text}

TRIGGERS (why you were called):
{trigger_text}

SENTIMENT:
{fng}

PORTFOLIO: {cash_line}

Should any of these triggers warrant a full trade analysis? Consider:
- Is the move significant enough for a short-term trade?
- Is there likely a catalyst or is this noise?
- Does the portfolio have cash to deploy?

Reply with EXACTLY one line:
ESCALATE: <symbol> | <one sentence why>
or
PASS: <one sentence why not>

You may include multiple ESCALATE lines if several assets look interesting."""

    return call_haiku(prompt, max_tokens=256)


def haiku_triage_position(state: dict, prices: dict, reason: str) -> str:
    """Ask Haiku whether to hold, exit, or escalate for full analysis."""
    pos = state["position"]
    current = prices.get(pos["symbol"], 0)
    entry = pos["entry_price"]
    pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0
    pnl_eur = pos["amount_eur"] * (pnl_pct / 100)
    high = pos.get("high_since_entry", entry)

    prompt = f"""You manage a volatile trade position for a 300EUR portfolio.

POSITION:
  Asset: {pos['symbol']}
  Entry: EUR {entry:.2f}
  Current: EUR {current:.2f}
  P&L: {pnl_pct:+.2f}% (EUR {pnl_eur:+.2f})
  High since entry: EUR {high:.2f}
  Stop-loss: EUR {pos['stop_loss']:.2f}
  Take-profit: {pos.get('take_profit', [])}
  Amount invested: EUR {pos['amount_eur']:.2f}
  Thesis: {pos.get('thesis', 'N/A')}

TRIGGER: {reason}

Should we EXIT now, HOLD the position, or ESCALATE to full analysis?

Reply with EXACTLY one line:
EXIT: <one sentence why>
HOLD: <one sentence why>
ESCALATE: <one sentence why full analysis is needed>"""

    return call_haiku(prompt, max_tokens=256)


# ---------------------------------------------------------------------------
# Sonnet decisions
# ---------------------------------------------------------------------------

def sonnet_entry_decision(symbol: str, prices: dict, state: dict) -> str:
    """Full Sonnet analysis for entry decision on a specific asset."""
    current_price = prices.get(symbol, 0)
    portfolio = read_file_from_repo("portfolio.md")
    strategy = read_file_from_repo("CLAUDE.md")
    fng = get_fear_greed_index()
    news = get_market_news()

    # Get price history for context
    history_text = "No history"
    for w in state["watchlist"]:
        if w["symbol"] == symbol:
            hist = w.get("price_history", [])
            if hist:
                history_text = " → ".join(f"€{p:.2f}" for p in hist[-6:])
            break

    prompt = f"""You are the volatile trading analyst for a 300EUR portfolio on Trade Republic.

## Asset Under Analysis
Symbol: {symbol}
Current price: EUR {current_price:.2f}
Recent price trajectory (10-min intervals): {history_text}

## Market Context
{fng}

Recent news:
{news}

## Portfolio
{portfolio}

## Strategy Rules (summary)
- Max loss per trade: 5% of portfolio (EUR 15)
- First trade in asset: max 20% of portfolio (EUR 60)
- Stop-loss REQUIRED
- Min reward:risk 2:1
- Max 3 open positions
- This is a SHORT-TERM volatile trade (hours to days, not weeks)

## Your Task
Decide whether to enter a short-term trade on {symbol} right now.

If YES, respond:
SIGNAL:
- ACTION: BUY
- ASSET: {symbol}
- AMOUNT: EUR amount (respect position sizing)
- ENTRY: current market price
- STOP-LOSS: specific price (max 5% portfolio loss)
- TAKE-PROFIT: specific price(s) (min 2:1 reward:risk)
- TIMEFRAME: expected hold (hours to days)
- THESIS: 2-3 sentences with specific reasoning
- TRAILING_STOP: percentage to activate after first TP (suggest 2-3%)

If NO, respond:
NO_SIGNAL: one sentence on why and what would change your mind.

Be specific. This is real money. Short-term volatile trades need tight stops and clear catalysts."""

    return call_sonnet(prompt, max_tokens=1024)


def sonnet_exit_decision(state: dict, prices: dict) -> str:
    """Full Sonnet analysis for exit decision."""
    pos = state["position"]
    current = prices.get(pos["symbol"], 0)
    entry = pos["entry_price"]
    pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0
    pnl_eur = pos["amount_eur"] * (pnl_pct / 100)
    high = pos.get("high_since_entry", entry)

    # Time held
    try:
        entry_dt = datetime.fromisoformat(pos["entry_time"])
        hours_held = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
    except (ValueError, TypeError, KeyError):
        hours_held = 0

    fng = get_fear_greed_index()
    news = get_market_news()
    portfolio = read_file_from_repo("portfolio.md")

    # Price history
    history_text = "No history"
    for w in state["watchlist"]:
        if w["symbol"] == pos["symbol"]:
            hist = w.get("price_history", [])
            if hist:
                history_text = " → ".join(f"€{p:.2f}" for p in hist[-6:])
            break

    prompt = f"""You manage a volatile trade position for a 300EUR portfolio.

## Current Position
Asset: {pos['symbol']}
Entry: EUR {entry:.2f} ({pos.get('entry_time', 'unknown')})
Current: EUR {current:.2f}
P&L: {pnl_pct:+.2f}% (EUR {pnl_eur:+.2f})
High since entry: EUR {high:.2f}
Hours held: {hours_held:.1f}
Amount: EUR {pos['amount_eur']:.2f}
Stop-loss: EUR {pos['stop_loss']:.2f}
Take-profit targets: {pos.get('take_profit', [])}
Original thesis: {pos.get('thesis', 'N/A')}

## Price Trajectory (recent 10-min intervals)
{history_text}

## Market Context
{fng}

Recent news:
{news}

## Portfolio
{portfolio}

## Your Task
Should we EXIT or HOLD this position?

If EXIT, respond:
EXIT:
- CURRENT PRICE: EUR {current:.2f}
- P&L: {pnl_pct:+.2f}% (EUR {pnl_eur:+.2f})
- REASON: why exit now
- ACTION: sell at market

If HOLD, respond:
HOLD:
- CURRENT PRICE: EUR {current:.2f}
- ADJUSTED STOP-LOSS: new stop-loss if warranted (e.g., raise to lock in profits)
- ADJUSTED TAKE-PROFIT: new targets if warranted
- TRAILING_STOP: percentage if you want to activate trailing stop
- REASON: why continue holding

Be honest. Protecting capital beats being right."""

    return call_sonnet(prompt, max_tokens=800)


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def handle_stop_loss(state: dict, prices: dict):
    """Immediate exit on stop-loss hit."""
    pos = state["position"]
    current = prices.get(pos["symbol"], 0)
    entry = pos["entry_price"]
    pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0
    pnl_eur = pos["amount_eur"] * (pnl_pct / 100)

    now = datetime.now(timezone.utc)
    message = (
        f"**VOLATILE EXIT — STOP-LOSS HIT** ({now.strftime('%H:%M UTC')})\n\n"
        f"Asset: {pos['symbol']}\n"
        f"Entry: EUR {entry:.2f}\n"
        f"Exit: EUR {current:.2f}\n"
        f"P&L: {pnl_pct:+.2f}% (EUR {pnl_eur:+.2f})\n"
        f"Amount: EUR {pos['amount_eur']:.2f}\n\n"
        f"SELL at market immediately."
    )
    send_discord(message)
    print(f"  STOP-LOSS HIT — sent exit signal")

    # Enter cooldown
    state["position"] = None
    state["mode"] = "COOLDOWN"
    state["cooldown_until"] = (now + timedelta(hours=COOLDOWN_HOURS)).isoformat()


def handle_take_profit(state: dict, prices: dict, level: int):
    """Handle take-profit hit."""
    pos = state["position"]
    current = prices.get(pos["symbol"], 0)
    entry = pos["entry_price"]
    pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0
    pnl_eur = pos["amount_eur"] * (pnl_pct / 100)

    now = datetime.now(timezone.utc)

    # If Haiku is available, ask whether to fully exit or set trailing stop
    if can_call(state, "haiku"):
        prompt = (
            f"Take-profit level {level + 1} hit on {pos['symbol']}. "
            f"Entry EUR {entry:.2f}, current EUR {current:.2f}, P&L {pnl_pct:+.2f}%. "
            f"Should we: (A) take full profit now, or (B) set a {TRAILING_STOP_PCT}% trailing stop "
            f"to capture more upside? Reply A or B with one sentence."
        )
        increment_daily_calls(state, "haiku")
        state["last_haiku_call"] = now.isoformat()
        haiku_result = call_haiku(prompt, max_tokens=128)
        print(f"  Haiku TP decision: {haiku_result}")

        if "B" in haiku_result.upper() or "TRAILING" in haiku_result.upper():
            # Set trailing stop
            pos["trailing_stop_pct"] = TRAILING_STOP_PCT
            message = (
                f"**VOLATILE — TAKE-PROFIT {level + 1} HIT** ({now.strftime('%H:%M UTC')})\n\n"
                f"Asset: {pos['symbol']}\n"
                f"Entry: EUR {entry:.2f} → Current: EUR {current:.2f}\n"
                f"P&L: {pnl_pct:+.2f}% (EUR {pnl_eur:+.2f})\n\n"
                f"Setting {TRAILING_STOP_PCT}% trailing stop to capture more upside.\n"
                f"Trail level: EUR {current * (1 - TRAILING_STOP_PCT / 100):.2f}"
            )
            send_discord(message)
            print(f"  TP hit — trailing stop set at {TRAILING_STOP_PCT}%")
            return

    # Default: full exit
    message = (
        f"**VOLATILE EXIT — TAKE-PROFIT HIT** ({now.strftime('%H:%M UTC')})\n\n"
        f"Asset: {pos['symbol']}\n"
        f"Entry: EUR {entry:.2f}\n"
        f"Exit: EUR {current:.2f}\n"
        f"P&L: {pnl_pct:+.2f}% (EUR {pnl_eur:+.2f})\n"
        f"Amount: EUR {pos['amount_eur']:.2f}\n\n"
        f"SELL at market to lock in profit."
    )
    send_discord(message)
    print(f"  TAKE-PROFIT HIT — sent exit signal")

    state["position"] = None
    state["mode"] = "COOLDOWN"
    state["cooldown_until"] = (now + timedelta(hours=COOLDOWN_HOURS)).isoformat()


def handle_trailing_stop(state: dict, prices: dict, trail_level: float):
    """Handle trailing stop trigger."""
    pos = state["position"]
    current = prices.get(pos["symbol"], 0)
    entry = pos["entry_price"]
    high = pos.get("high_since_entry", entry)
    pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0
    pnl_eur = pos["amount_eur"] * (pnl_pct / 100)

    now = datetime.now(timezone.utc)
    message = (
        f"**VOLATILE EXIT — TRAILING STOP** ({now.strftime('%H:%M UTC')})\n\n"
        f"Asset: {pos['symbol']}\n"
        f"Entry: EUR {entry:.2f}\n"
        f"High: EUR {high:.2f}\n"
        f"Trail level: EUR {trail_level:.2f}\n"
        f"Exit: EUR {current:.2f}\n"
        f"P&L: {pnl_pct:+.2f}% (EUR {pnl_eur:+.2f})\n"
        f"Amount: EUR {pos['amount_eur']:.2f}\n\n"
        f"SELL at market — trailing stop triggered."
    )
    send_discord(message)
    print(f"  TRAILING STOP — sent exit signal")

    state["position"] = None
    state["mode"] = "COOLDOWN"
    state["cooldown_until"] = (now + timedelta(hours=COOLDOWN_HOURS)).isoformat()


def handle_entry_signal(state: dict, signal_text: str, symbol: str, prices: dict):
    """Process a Sonnet entry signal and send to Discord."""
    now = datetime.now(timezone.utc)

    # Parse key fields from signal
    import re
    amount_match = re.search(r"AMOUNT:\s*EUR?\s*([\d.]+)", signal_text, re.IGNORECASE)
    stop_match = re.search(r"STOP-LOSS:\s*(?:EUR?\s*)?([\d.]+)", signal_text, re.IGNORECASE)
    tp_matches = re.findall(r"TAKE-PROFIT:.*?(?:EUR?\s*)?([\d.]+)", signal_text, re.IGNORECASE)
    trail_match = re.search(r"TRAILING_STOP:\s*([\d.]+)%?", signal_text, re.IGNORECASE)
    thesis_match = re.search(r"THESIS:\s*(.+?)(?:\n|$)", signal_text, re.IGNORECASE)
    timeframe_match = re.search(r"TIMEFRAME:\s*(.+?)(?:\n|$)", signal_text, re.IGNORECASE)

    current_price = prices.get(symbol, 0)
    amount = float(amount_match.group(1)) if amount_match else 50.0
    stop_loss = float(stop_match.group(1)) if stop_match else current_price * 0.95
    take_profits = [float(tp) for tp in tp_matches] if tp_matches else [current_price * 1.05]
    trailing = float(trail_match.group(1)) if trail_match else None
    thesis = thesis_match.group(1).strip() if thesis_match else "See signal details"
    timeframe = timeframe_match.group(1).strip() if timeframe_match else "hours to days"

    # Create position in state
    state["position"] = {
        "symbol": symbol,
        "entry_price": current_price,
        "entry_time": now.isoformat(),
        "amount_eur": amount,
        "stop_loss": stop_loss,
        "take_profit": take_profits,
        "trailing_stop_pct": trailing,
        "thesis": thesis,
        "timeframe": timeframe,
        "last_check_price": current_price,
        "high_since_entry": current_price,
    }
    state["mode"] = "IN_POSITION"

    # Send to Discord
    message = (
        f"**VOLATILE ENTRY SIGNAL** ({now.strftime('%H:%M UTC')})\n\n"
        f"{signal_text[:1800]}"
    )
    send_discord(message)
    print(f"  ENTRY SIGNAL sent for {symbol}")


def handle_exit_from_analysis(state: dict, prices: dict, analysis: str):
    """Process a Sonnet/Haiku exit decision."""
    pos = state["position"]
    current = prices.get(pos["symbol"], 0)
    entry = pos["entry_price"]
    pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0
    pnl_eur = pos["amount_eur"] * (pnl_pct / 100)

    now = datetime.now(timezone.utc)
    message = (
        f"**VOLATILE EXIT SIGNAL** ({now.strftime('%H:%M UTC')})\n\n"
        f"Asset: {pos['symbol']}\n"
        f"Entry: EUR {entry:.2f} → Current: EUR {current:.2f}\n"
        f"P&L: {pnl_pct:+.2f}% (EUR {pnl_eur:+.2f})\n\n"
        f"{analysis[:1200]}\n\n"
        f"SELL at market."
    )
    send_discord(message)
    print(f"  EXIT SIGNAL sent")

    state["position"] = None
    state["mode"] = "COOLDOWN"
    state["cooldown_until"] = (now + timedelta(hours=COOLDOWN_HOURS)).isoformat()


def handle_hold_adjustments(state: dict, analysis: str):
    """Parse hold response for stop-loss/TP adjustments."""
    import re
    pos = state["position"]

    # Check for adjusted stop-loss
    sl_match = re.search(r"ADJUSTED STOP-LOSS:\s*(?:EUR?\s*)?([\d.]+)", analysis, re.IGNORECASE)
    if sl_match:
        new_sl = float(sl_match.group(1))
        if new_sl > pos["stop_loss"]:  # Only tighten, never loosen
            print(f"  Raising stop-loss: {pos['stop_loss']:.2f} → {new_sl:.2f}")
            pos["stop_loss"] = new_sl

    # Check for trailing stop activation
    trail_match = re.search(r"TRAILING_STOP:\s*([\d.]+)%?", analysis, re.IGNORECASE)
    if trail_match:
        pos["trailing_stop_pct"] = float(trail_match.group(1))
        print(f"  Trailing stop set at {pos['trailing_stop_pct']}%")


# ---------------------------------------------------------------------------
# Watchlist management
# ---------------------------------------------------------------------------

def manage_watchlist(state: dict, action: str, symbol: str):
    """Add or remove assets from watchlist."""
    now = datetime.now(timezone.utc)

    if action == "add_watch":
        # Check if already in watchlist
        existing = [w["symbol"] for w in state["watchlist"]]
        if symbol in existing:
            print(f"  {symbol} already in watchlist")
            return

        # Determine source
        crypto_symbols = [
            "bitcoin", "ethereum", "solana", "ripple", "cardano",
            "avalanche-2", "chainlink", "polkadot", "dogecoin", "shiba-inu",
        ]
        if symbol.lower() in crypto_symbols:
            source = "coingecko"
            yahoo_sym = None
        else:
            source = "yahoo"
            yahoo_sym = symbol.upper()

        state["watchlist"].append({
            "symbol": symbol.lower() if source == "coingecko" else symbol,
            "source": source,
            "yahoo_symbol": yahoo_sym,
            "last_price": None,
            "price_history": [],
            "tracked_since": now.isoformat(),
        })
        print(f"  Added {symbol} to watchlist ({source})")
        send_discord(f"**Volatile Tracker**: Added {symbol} to watchlist")

    elif action == "remove_watch":
        before = len(state["watchlist"])
        state["watchlist"] = [
            w for w in state["watchlist"]
            if w["symbol"].lower() != symbol.lower()
        ]
        if len(state["watchlist"]) < before:
            print(f"  Removed {symbol} from watchlist")
            send_discord(f"**Volatile Tracker**: Removed {symbol} from watchlist")
        else:
            print(f"  {symbol} not found in watchlist")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)
    print(f"[{now.isoformat()}] Volatile tracker starting...")

    # Load state
    state = load_state()
    state["last_run"] = now.isoformat()
    state_changed = True  # Always save updated last_run + prices

    # Handle manual actions
    action = os.environ.get("ACTION", "run")
    symbol = os.environ.get("SYMBOL", "")

    if action in ("add_watch", "remove_watch") and symbol:
        manage_watchlist(state, action, symbol)
        save_state(state)
        print("Done.")
        return

    if action == "force_exit" and state["position"]:
        pos = state["position"]
        send_discord(
            f"**VOLATILE — FORCED EXIT** ({now.strftime('%H:%M UTC')})\n\n"
            f"Asset: {pos['symbol']}\n"
            f"Manually forced exit. SELL at market."
        )
        state["position"] = None
        state["mode"] = "COOLDOWN"
        state["cooldown_until"] = (now + timedelta(hours=COOLDOWN_HOURS)).isoformat()
        save_state(state)
        print("Forced exit. Done.")
        return

    # Check cooldown
    if state["mode"] == "COOLDOWN":
        cooldown_until = state.get("cooldown_until")
        if cooldown_until:
            try:
                cd_dt = datetime.fromisoformat(cooldown_until)
                if now >= cd_dt:
                    print("  Cooldown expired → WATCHING")
                    state["mode"] = "WATCHING"
                    state["cooldown_until"] = None
                else:
                    remaining = (cd_dt - now).total_seconds() / 60
                    print(f"  In cooldown for {remaining:.0f} more minutes")
                    save_state(state)
                    print("Done.")
                    return
            except (ValueError, TypeError):
                state["mode"] = "WATCHING"
                state["cooldown_until"] = None

    # Fetch prices
    print("\nFetching watchlist prices...")
    prices = fetch_watchlist_prices(state["watchlist"])
    if not prices:
        print("  No prices fetched — skipping this run")
        save_state(state)
        print("Done.")
        return

    print("  Prices:", {k: f"€{v:.2f}" for k, v in prices.items()})

    # Update price history
    update_price_history(state["watchlist"], prices)

    # === WATCHING MODE ===
    if state["mode"] == "WATCHING":
        print("\nMode: WATCHING")

        # Pre-filter
        triggers = pre_filter_watching(state, prices)
        if not triggers:
            print("  No triggers — holding steady")
            save_state(state)
            print("Done.")
            return

        print(f"  Triggers: {triggers}")

        # Can we call Haiku?
        if not can_call(state, "haiku"):
            print("  Haiku daily cap reached — skipping")
            save_state(state)
            print("Done.")
            return

        # Haiku triage
        print("\n  Calling Haiku for triage...")
        increment_daily_calls(state, "haiku")
        state["last_haiku_call"] = now.isoformat()
        haiku_result = haiku_triage_watching(state, prices, triggers)
        print(f"  Haiku: {haiku_result}")

        # Parse Haiku response
        if "ESCALATE" in haiku_result.upper():
            # Extract symbol to research
            import re
            escalate_match = re.search(
                r"ESCALATE:\s*(\S+)", haiku_result, re.IGNORECASE
            )
            escalate_sym = escalate_match.group(1).lower().strip("|,") if escalate_match else None

            # Map common names
            name_map = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana"}
            if escalate_sym and escalate_sym in name_map:
                escalate_sym = name_map[escalate_sym]

            # Fallback: use first trigger symbol
            if not escalate_sym or escalate_sym == "escalate":
                escalate_sym = triggers[0][0] if triggers else None

            if escalate_sym and escalate_sym != "periodic" and can_call(state, "sonnet"):
                print(f"\n  Escalating to Sonnet for {escalate_sym}...")
                increment_daily_calls(state, "sonnet")
                sonnet_result = sonnet_entry_decision(escalate_sym, prices, state)
                print(f"  Sonnet: {sonnet_result[:200]}...")

                if "SIGNAL:" in sonnet_result and "NO_SIGNAL:" not in sonnet_result:
                    handle_entry_signal(state, sonnet_result, escalate_sym, prices)
                else:
                    print("  No signal from Sonnet")
            else:
                print(f"  Cannot escalate (sym={escalate_sym}, sonnet_ok={can_call(state, 'sonnet')})")
        else:
            print("  Haiku says PASS")

    # === IN_POSITION MODE ===
    elif state["mode"] == "IN_POSITION" and state["position"]:
        pos = state["position"]
        current = prices.get(pos["symbol"], 0)
        print(f"\nMode: IN_POSITION ({pos['symbol']} @ €{pos['entry_price']:.2f}, now €{current:.2f})")

        # Pre-filter
        result = pre_filter_in_position(state, prices)
        print(f"  Pre-filter: {result}")

        if result["action"] == "stop_loss":
            handle_stop_loss(state, prices)

        elif result["action"] == "take_profit":
            handle_take_profit(state, prices, result["level"])

        elif result["action"] == "trailing_stop":
            handle_trailing_stop(state, prices, result["trail_level"])

        elif result["action"] == "haiku":
            if can_call(state, "haiku"):
                print("\n  Calling Haiku for position check...")
                increment_daily_calls(state, "haiku")
                state["last_haiku_call"] = now.isoformat()
                haiku_result = haiku_triage_position(state, prices, result["reason"])
                print(f"  Haiku: {haiku_result}")

                if "EXIT" in haiku_result.upper() and "ESCALATE" not in haiku_result.upper():
                    handle_exit_from_analysis(state, prices, haiku_result)

                elif "ESCALATE" in haiku_result.upper():
                    if can_call(state, "sonnet"):
                        print("\n  Escalating to Sonnet for exit decision...")
                        increment_daily_calls(state, "sonnet")
                        sonnet_result = sonnet_exit_decision(state, prices)
                        print(f"  Sonnet: {sonnet_result[:200]}...")

                        if "EXIT" in sonnet_result.upper():
                            handle_exit_from_analysis(state, prices, sonnet_result)
                        else:
                            handle_hold_adjustments(state, sonnet_result)
                            print("  Sonnet says HOLD")
                    else:
                        print("  Sonnet daily cap reached")
                else:
                    # HOLD
                    handle_hold_adjustments(state, haiku_result)
                    print("  Haiku says HOLD")
            else:
                print("  Haiku daily cap reached — rule-based only")

        # Update last check price
        if state["position"]:
            state["position"]["last_check_price"] = current

    # Save state
    save_state(state)
    print("\nDone.")


if __name__ == "__main__":
    main()
