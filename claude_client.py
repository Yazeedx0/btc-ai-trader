"""
Claude integration — sends market context to Claude and parses the
structured JSON trading decision.  Includes a memory system that feeds
Claude its recent trade history so it can learn from mistakes.
"""

from __future__ import annotations

import csv
import json
import os
from typing import Any

import anthropic
import pandas as pd

import config


_client: anthropic.Anthropic | None = None
# In-memory ring buffer of recent decisions + outcomes
_trade_memory: list[dict] = []
MAX_MEMORY = 20


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.CLAUDE_API_KEY)
    return _client


# ── Payload builder ──────────────────────────────────────────────────

def _compress_candles(df: pd.DataFrame) -> list[list]:
    """Reduce candle data to a compact list-of-lists for the prompt."""
    cols = ["open_time", "open", "high", "low", "close", "volume"]
    rows = []
    for _, r in df[cols].iterrows():
        rows.append([
            str(r["open_time"]),
            round(r["open"], 2),
            round(r["high"], 2),
            round(r["low"], 2),
            round(r["close"], 2),
            round(r["volume"], 4),
        ])
    return rows


def build_payload(
    df: pd.DataFrame,
    account: dict,
    recent_trades: list[dict],
    indicators: dict | None = None,
    market_sentiment: dict | None = None,
    multi_tf: dict | None = None,
) -> dict:
    """Build the structured JSON payload sent inside the Claude prompt."""
    latest = df.iloc[-1]
    payload = {
        "symbol": config.SYMBOL,
        "timeframe": config.TIMEFRAME,
        "balance": account["usdt_balance"],
        "open_positions": account["positions"],
        "unrealized_pnl": account["unrealized_pnl"],
        "recent_trades": recent_trades,
        "current_price": round(float(latest["close"]), 2),
        "candles": _compress_candles(df),
    }

    # ── Indicators (full set) ────────────────────────────────────
    if indicators:
        payload["indicators"] = indicators
    else:
        # Fallback to basic
        payload["indicators"] = {
            "ema20": round(float(latest["ema20"]), 2),
            "ema50": round(float(latest["ema50"]), 2),
            "rsi14": round(float(latest["rsi14"]), 2),
            "atr14": round(float(latest["atr14"]), 2),
        }

    # ── Market Sentiment ─────────────────────────────────────────
    if market_sentiment:
        payload["market_sentiment"] = market_sentiment

    # ── Multi-Timeframe (ALL timeframes for Claude's analysis) ───
    if multi_tf:
        payload["timeframes"] = multi_tf

    # ── Inject memory ────────────────────────────────────────────
    if _trade_memory:
        payload["your_trade_history"] = _trade_memory
        payload["performance_summary"] = _compute_performance_summary()

    return payload


# ── Memory helpers ───────────────────────────────────────────────────

def record_decision(decision: dict, pnl: float | None = None,
                    entry_price: float | None = None,
                    fees: float | None = None) -> None:
    """Call after each cycle to record what Claude decided and how it went."""
    entry = {
        "action": decision.get("action"),
        "confidence": decision.get("confidence"),
        "leverage": decision.get("leverage"),
        "position_size_percent": decision.get("position_size_percent"),
        "stop_loss": decision.get("stop_loss"),
        "take_profit": decision.get("take_profit"),
        "comment": decision.get("comment"),
        "result_pnl": pnl,
        "entry_price": entry_price,
        "fees": fees,
    }
    _trade_memory.append(entry)
    if len(_trade_memory) > MAX_MEMORY:
        _trade_memory.pop(0)


def _compute_performance_summary() -> dict:
    """Compute stats from the memory buffer so Claude sees its track record."""
    trades_with_pnl = [t for t in _trade_memory if t.get("result_pnl") is not None]
    if not trades_with_pnl:
        return {"total_trades": 0}

    wins = [t for t in trades_with_pnl if t["result_pnl"] > 0]
    losses = [t for t in trades_with_pnl if t["result_pnl"] <= 0]
    total_pnl = sum(t["result_pnl"] for t in trades_with_pnl)
    total_fees = sum(t.get("fees", 0) or 0 for t in trades_with_pnl)

    # Streak tracking
    streak = 0
    for t in reversed(trades_with_pnl):
        if t["result_pnl"] <= 0:
            streak -= 1
        else:
            streak += 1
            break

    # What setups lost money
    losing_patterns = []
    for t in losses[-5:]:
        losing_patterns.append(f"{t['action']} (confidence={t['confidence']}, pnl={t['result_pnl']:.1f})")

    return {
        "total_trades": len(trades_with_pnl),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades_with_pnl) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "total_fees_paid": round(total_fees, 2),
        "avg_win": round(sum(t["result_pnl"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t["result_pnl"] for t in losses) / len(losses), 2) if losses else 0,
        "current_streak": streak,
        "recent_losing_trades": losing_patterns,
        "lesson": "LEARN FROM YOUR LOSSES. Do NOT repeat losing patterns. If high leverage + big size lost money, USE LESS. If a setup kept failing, AVOID IT.",
    }


# ── System prompt ────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an elite BTCUSDT futures trader. You run every 5 minutes.
You receive FULL multi-timeframe data (1m, 5m, 15m, 1h, 4h) — YOU decide which timeframe matters most.

═══ YOUR JOB ═══
1. DETERMINE THE OVERALL MARKET DIRECTION from the higher timeframes (4h → 1h → 15m).
2. CHOOSE which timeframe drives your trade decision (e.g. 4h for trend, 5m for entry).
3. TRADE with precision — be aggressive when signals align, patient when they don't.

═══ MEMORY SYSTEM ═══
"your_trade_history" = your last 20 decisions + PnL. "performance_summary" = stats.
STUDY them. If you keep losing on a pattern → STOP doing it. If something works → press it.

═══ DATA YOU RECEIVE ═══

1. MULTI-TIMEFRAME DATA (in "timeframes"):
   Each TF (1m, 5m, 15m, 1h, 4h) has: EMA9/20/50, RSI14, StochRSI, MACD, BB, ATR, volume, trend summary.
   - "trend": BULLISH (ema9>ema20>ema50) | BEARISH (ema9<ema20<ema50) | MIXED
   - "macd_cross": BULLISH/BEARISH/NONE — histogram crossing zero
   - "momentum": STRONG/WEAK based on MACD vs ATR
   - "bb_position": UPPER/LOWER/MIDDLE
   - "last5_green": how many of last 5 candles were green (0-5)

   HOW TO READ MULTI-TF:
   - If 4h+1h are BULLISH, the macro trend is UP → prefer longs
   - If 4h+1h are BEARISH, the macro trend is DOWN → prefer shorts
   - If they disagree → be careful, reduce size, or HOLD
   - Use 15m/5m for entry timing, 1m for precision
   - 4h RSI > 70 + 1h RSI > 70 = overbought across timeframes → reversal risk
   - All TFs trending same direction = HIGH CONFIDENCE setup

2. MARKET SENTIMENT (in "market_sentiment"):
   - funding_rate: Extreme positive (>0.03) → longs crowded → short opportunity.
     Extreme negative (<-0.03) → shorts crowded → long opportunity.
   - open_interest: Rising OI + rising price = strong trend. Rising OI + falling price = distribution.
   - long_short_ratio: Extreme values signal crowded trades → potential reversal.
   - order_book: imbalance_pct + pressure direction.
   - realtime_flow: LIVE WebSocket buy/sell pressure (last 10 seconds). Most current data.

3. CURRENT 5m INDICATORS (in "indicators"):
   Full indicator set for the base 5m timeframe including VWAP, BB, StochRSI, etc.

═══ STRATEGY FRAMEWORK ═══

Step 1: MACRO DIRECTION (4h + 1h)
  → Determine if market is trending UP, DOWN, or RANGING
  → This sets your BIAS (long-only, short-only, or both)

Step 2: INTERMEDIATE CONFIRMATION (15m)
  → Does 15m agree with 4h/1h? If yes → high conviction
  → 15m diverging from 1h = potential reversal starting

Step 3: ENTRY TIMING (5m + 1m)
  → Use 5m for setup, 1m for precision entry
  → Wait for pullback to EMA20 in the trend direction
  → StochRSI oversold in uptrend = BUY. Overbought in downtrend = SELL.

Step 4: SIZE & RISK BASED ON ALIGNMENT
  → ALL TFs agree: leverage 15-20x, size 60-80%
  → 3 TFs agree: leverage 10-15x, size 40-60%
  → Mixed signals: leverage 5-10x, size 20-40% or HOLD
  → Counter-trend: HOLD or tiny size (10-20%, 3-5x)

═══ RISK RULES ═══
- Stop loss: 0.5-1x ATR from entry. NEVER trade without SL.
- Take profit: 2-3x ATR (let winners run).
- If open position is losing > 1.5x ATR → CLOSE.
- On a 3+ losing streak → reduce leverage to 5x, size to 20%.
- CONFLUENCE IS KING: 3+ signals from different TFs must agree.
- Don't overtrade. HOLD is a valid decision. Patience = profit.

═══ OUTPUT FORMAT ═══
Respond with EXACTLY this JSON (no markdown, no extra text):

{
  "action": "BUY | SELL | CLOSE | HOLD",
  "position_size_percent": <number 1-80>,
  "leverage": <number 1-20>,
  "stop_loss": <price as number>,
  "take_profit": <price as number>,
  "confidence": <number 0.0-1.0>,
  "timeframe_used": "<which TF drove your decision, e.g. '4h trend + 5m entry'>",
  "market_direction": "<BULLISH | BEARISH | RANGING>",
  "comment": "<explain: 1) overall market direction from higher TFs, 2) which signals aligned, 3) why this entry, 4) lessons from history>"
}

Rules:
- BUY = open LONG. SELL = open SHORT. CLOSE = close position. HOLD = skip.
- position_size_percent: Scale based on TF alignment (see Step 4 above).
- leverage: Scale based on conviction (see Step 4).
- timeframe_used: MUST state which TFs you analyzed and which drove the trade.
- market_direction: Your assessment of the overall market from higher TFs.
- Respond ONLY with the JSON object."""


# ── Call Claude ───────────────────────────────────────────────────────

def get_decision(
    df: pd.DataFrame,
    account: dict,
    recent_trades: list[dict],
    indicators: dict | None = None,
    market_sentiment: dict | None = None,
    multi_tf: dict | None = None,
) -> dict | None:
    """
    Send market context to Claude and return the parsed decision dict,
    or None if the response is not valid JSON.
    """
    payload = build_payload(df, account, recent_trades,
                            indicators=indicators,
                            market_sentiment=market_sentiment,
                            multi_tf=multi_tf)
    client = _get_client()

    try:
        message = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": json.dumps(payload)},
            ],
        )
        raw_text = message.content[0].text.strip()

        # Try to extract JSON even if Claude wraps it in markdown fences
        if raw_text.startswith("```"):
            # Strip ```json ... ```
            lines = raw_text.splitlines()
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw_text = "\n".join(lines).strip()

        decision = json.loads(raw_text)

        # Basic schema validation
        required_keys = {
            "action", "position_size_percent", "leverage",
            "stop_loss", "take_profit", "confidence",
        }
        if not required_keys.issubset(decision.keys()):
            print(f"[CLAUDE] Missing keys in response: {required_keys - decision.keys()}")
            return None

        if decision["action"] not in ("BUY", "SELL", "CLOSE", "HOLD"):
            print(f"[CLAUDE] Invalid action: {decision['action']}")
            return None

        # Log Claude's market read
        direction = decision.get("market_direction", "?")
        tf_used = decision.get("timeframe_used", "?")
        print(f"[CLAUDE] Market direction: {direction} | Timeframe used: {tf_used}")

        return decision

    except json.JSONDecodeError as exc:
        print(f"[CLAUDE] Invalid JSON response: {exc}")
        return None
    except anthropic.APIError as exc:
        print(f"[CLAUDE] API error: {exc}")
        return None
    except Exception as exc:
        print(f"[CLAUDE] Unexpected error: {exc}")
        return None
