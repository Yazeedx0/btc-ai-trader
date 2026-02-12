"""
Gemini integration — sends market context to Google Gemini and parses
the structured JSON trading decision.

Dual-model approach:
  - Gemini Pro  → Full 5m analysis (deep reasoning, large context)
  - Gemini Flash → Quick 1m exit checks (fast, cheap, low latency)

Includes a memory system that feeds Gemini its recent trade history
so it can learn from mistakes.
"""

from __future__ import annotations

import json
import time
from typing import Any

from google import genai
from google.genai import types
import pandas as pd

import config


_client: genai.Client | None = None
# In-memory ring buffer of recent decisions + outcomes
_trade_memory: list[dict] = []
MAX_MEMORY = 20


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
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
    """Build the structured JSON payload sent inside the Gemini prompt."""
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

    # ── Multi-Timeframe (ALL timeframes for analysis) ────────────
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
    """Call after each cycle to record what Gemini decided and how it went."""
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
    """Compute stats from the memory buffer so Gemini sees its track record."""
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

SYSTEM_PROMPT = """You are an elite BTCUSDT futures trader. You run every 5 minutes (full analysis) and every 1 minute (position check).
You receive FULL multi-timeframe data (1m, 5m, 15m, 1h, 4h) — YOU decide which timeframe matters most.

═══ CRITICAL: YOU CONTROL ALL EXITS ═══
There are NO stop-loss or take-profit orders on the exchange.
YOU are the stop-loss. YOU are the take-profit. YOU decide when to close.
This means: if you don't close a losing trade, it stays open and bleeds money.
If you don't hold a winning trade, you miss the move.

═══ YOUR MODES ═══
1. FULL ANALYSIS (every 5m): You get all TF data. Decide: BUY, SELL, ADD, CLOSE, or HOLD.
2. QUICK CHECK (every 1m): You have an open position. Check if you should CLOSE or HOLD.
   Quick check payload has "mode": "quick_check" with position details and 1m/5m data.

═══ MEMORY SYSTEM ═══
"your_trade_history" = your last 20 decisions + PnL. "performance_summary" = stats.
STUDY them. Learn. Adapt. If a pattern keeps losing → stop. If winning → press harder.

═══ DATA YOU RECEIVE ═══

1. MULTI-TIMEFRAME DATA (in "timeframes"): 1m, 5m, 15m, 1h, 4h
   Each with: EMA9/20/50, RSI14, StochRSI, MACD, BB, ATR, volume, trend, momentum

2. MARKET SENTIMENT: funding_rate, open_interest, long_short_ratio, order_book, realtime_flow

3. POSITION INFO (when open):
   "open_position": {side, entry_price, size, unrealized_pnl, leverage, pnl_vs_atr}
   - pnl_vs_atr: how many ATRs the position has moved. >2 = great winner. <-1 = cut it.

═══ STRATEGY: TRAILING STOP (MOST IMPORTANT) ═══

When you have an OPEN WINNING position:
- pnl_vs_atr > 0.5: HOLD — move your mental stop to breakeven
- pnl_vs_atr > 1.0: HOLD — this is a good winner, let it run
- pnl_vs_atr > 2.0: HOLD — excellent! Consider adding (ADD action)
- pnl_vs_atr > 3.0: Start watching for reversal signs to lock profit
- CLOSE only when: price reverses through a key level, or momentum shifts on 5m+

When you have an OPEN LOSING position:
- pnl_vs_atr < -0.5: WARNING — watch closely
- pnl_vs_atr < -1.0: CLOSE immediately. Do NOT hope. Cut the loss.
- pnl_vs_atr < -0.3 AND trend reversed on 5m: CLOSE early

═══ PYRAMIDING (ADD action) ═══
When a trade is winning well (pnl_vs_atr > 1.5) and trend is still strong:
- Use ADD action to increase position size
- ADD max 2 times per trade
- Each ADD should be smaller than initial (20-30% of balance)
- Only ADD when the SAME signals that got you in are still valid

═══ ENTRY STRATEGY ═══

Step 1: FIND DIRECTION from 4h + 1h
Step 2: CONFIRM on 15m
Step 3: ENTER on 5m pullback (EMA20 bounce, StochRSI reset)
Step 4: SIZE based on alignment:
  → 3+ TFs agree: leverage 15-20x, size 50-80%
  → 2 TFs agree: leverage 10-15x, size 30-50%
  → Mixed: HOLD — do NOT force entries

═══ QUICK CHECK MODE ═══
When "mode" = "quick_check":
- You ONLY decide: CLOSE or HOLD
- Check if position should be closed based on trailing stop rules above
- Look at 1m and 5m data for momentum shift
- If winning → HOLD (let it run)
- If losing past threshold → CLOSE
- Be FAST: respond in 1-2 seconds

═══ OUTPUT FORMAT ═══
For FULL ANALYSIS — respond with EXACTLY this JSON:
{
  "action": "BUY | SELL | ADD | CLOSE | HOLD",
  "position_size_percent": <number 10-90>,
  "leverage": <number 5-20>,
  "stop_loss": <mental stop price — for your reference>,
  "take_profit": <target price — for your reference>,
  "confidence": <number 0.0-1.0>,
  "timeframe_used": "<which TFs drove your decision>",
  "market_direction": "<BULLISH | BEARISH | RANGING>",
  "comment": "<direction + signals + trailing stop logic>"
}

For QUICK CHECK — respond with EXACTLY this JSON:
{
  "action": "CLOSE | HOLD",
  "confidence": <number 0.0-1.0>,
  "comment": "<why close or hold — reference pnl_vs_atr>"
}

Rules:
- BUY = open LONG. SELL = open SHORT. ADD = add to winning position. CLOSE = close. HOLD = wait.
- ADD requires "add_size_percent": <10-30> in the response.
- NO SL/TP on exchange — you ARE the risk manager. Cut losses at -1x ATR.
- Let winners run past 2-3x ATR before considering exit.
- Respond ONLY with the JSON object."""


# ── Quick check prompt (lighter, faster — used with Flash model) ─────

QUICK_CHECK_PROMPT = """You are managing an open BTCUSDT futures position.
Check if you should CLOSE or HOLD based on these rules:

TRAILING STOP:
- Winning > 2x ATR: HOLD (let it run, watch for reversal)
- Winning > 1x ATR: HOLD (move mental stop to breakeven)
- Winning > 0.5x ATR: HOLD (early, don't cut)
- Flat (near 0): HOLD (give it time)
- Losing > -0.5x ATR: WATCH closely
- Losing > -1x ATR: CLOSE immediately
- Momentum reversed on 1m+5m while losing: CLOSE

Respond with EXACTLY this JSON:
{"action": "CLOSE | HOLD", "confidence": <0.0-1.0>, "comment": "<reason>"}"""


# ── JSON response schemas for Gemini structured output ───────────────

_FULL_DECISION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "action": {"type": "STRING"},
        "position_size_percent": {"type": "NUMBER"},
        "leverage": {"type": "NUMBER"},
        "stop_loss": {"type": "NUMBER"},
        "take_profit": {"type": "NUMBER"},
        "confidence": {"type": "NUMBER"},
        "timeframe_used": {"type": "STRING"},
        "market_direction": {"type": "STRING"},
        "comment": {"type": "STRING"},
        "add_size_percent": {"type": "NUMBER"},
    },
    "required": [
        "action", "position_size_percent", "leverage",
        "stop_loss", "take_profit", "confidence",
    ],
}

_QUICK_CHECK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "action": {"type": "STRING"},
        "confidence": {"type": "NUMBER"},
        "comment": {"type": "STRING"},
    },
    "required": ["action", "confidence", "comment"],
}


# ── Call Gemini Pro (full 5m analysis) ────────────────────────────────

def _call_with_retry(model: str, contents: str, gen_config: types.GenerateContentConfig,
                     max_retries: int = 3) -> str | None:
    """Call Gemini with retry + exponential backoff. Returns response text or None."""
    client = _get_client()
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=gen_config,
            )
            if response.text is None:
                print(f"[GEMINI] Empty response from {model} (attempt {attempt + 1})")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return None
            return response.text.strip()
        except Exception as exc:
            err_str = str(exc)
            is_retryable = any(code in err_str for code in ("503", "429", "500", "UNAVAILABLE", "RESOURCE_EXHAUSTED"))
            print(f"[GEMINI] {model} error (attempt {attempt + 1}/{max_retries}): {err_str[:120]}")
            if is_retryable and attempt < max_retries - 1:
                wait = 2 ** attempt + 1
                print(f"[GEMINI] Retrying in {wait}s ...")
                time.sleep(wait)
            else:
                return None
    return None


def get_decision(
    df: pd.DataFrame,
    account: dict,
    recent_trades: list[dict],
    indicators: dict | None = None,
    market_sentiment: dict | None = None,
    multi_tf: dict | None = None,
) -> dict | None:
    """
    Send market context to Gemini Pro and return the parsed decision dict.
    Falls back to Flash if Pro is unavailable.
    """
    payload = build_payload(df, account, recent_trades,
                            indicators=indicators,
                            market_sentiment=market_sentiment,
                            multi_tf=multi_tf)
    contents = json.dumps(payload)
    gen_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0.3,
        max_output_tokens=1024,
        response_mime_type="application/json",
        response_schema=_FULL_DECISION_SCHEMA,
    )

    # Try Pro first, fallback to Flash
    raw_text = _call_with_retry(config.GEMINI_PRO_MODEL, contents, gen_config, max_retries=3)
    if raw_text is None:
        print("[GEMINI] Pro unavailable — falling back to Flash ...")
        raw_text = _call_with_retry(config.GEMINI_FLASH_MODEL, contents, gen_config, max_retries=2)
    if raw_text is None:
        print("[GEMINI] Both models failed.")
        return None

    try:
        decision = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        print(f"[GEMINI] Invalid JSON: {exc}")
        return None

    # Basic schema validation
    required_keys = {
        "action", "position_size_percent", "leverage",
        "stop_loss", "take_profit", "confidence",
    }
    if not required_keys.issubset(decision.keys()):
        print(f"[GEMINI] Missing keys: {required_keys - decision.keys()}")
        return None

    if decision["action"] not in ("BUY", "SELL", "CLOSE", "HOLD", "ADD"):
        print(f"[GEMINI] Invalid action: {decision['action']}")
        return None

    direction = decision.get("market_direction", "?")
    tf_used = decision.get("timeframe_used", "?")
    print(f"[GEMINI] Market direction: {direction} | Timeframe used: {tf_used}")
    return decision


# ── Call Gemini Flash (fast 1m quick check) ───────────────────────────

def get_quick_check(position_info: dict, indicators_1m: dict,
                    indicators_5m: dict) -> dict | None:
    """
    Quick 1-minute check: should we CLOSE or HOLD the open position?
    Uses Gemini Flash for speed + lower cost. Retries on failure.
    """
    payload = {
        "mode": "quick_check",
        "open_position": position_info,
        "indicators_1m": indicators_1m,
        "indicators_5m": indicators_5m,
    }
    if _trade_memory:
        payload["performance_summary"] = _compute_performance_summary()

    gen_config = types.GenerateContentConfig(
        system_instruction=QUICK_CHECK_PROMPT,
        temperature=0.1,
        max_output_tokens=256,
        response_mime_type="application/json",
        response_schema=_QUICK_CHECK_SCHEMA,
    )

    raw_text = _call_with_retry(config.GEMINI_FLASH_MODEL, json.dumps(payload), gen_config, max_retries=2)
    if raw_text is None:
        return None

    try:
        decision = json.loads(raw_text)
        if decision.get("action") not in ("CLOSE", "HOLD"):
            return None
        return decision
    except Exception as exc:
        print(f"[QUICK] Parse error: {exc}")
        return None
