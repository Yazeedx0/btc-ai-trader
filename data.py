"""
Data pipeline — fetches candles & account info from Binance Futures Testnet,
computes technical indicators, market sentiment data, and multi-timeframe analysis.
"""

from __future__ import annotations

import time
import hmac
import hashlib
from urllib.parse import urlencode
from typing import Any

import requests
import pandas as pd
import numpy as np

import config


# ── HTTP helpers ─────────────────────────────────────────────────────

def _sign(params: dict) -> dict:
    """Add timestamp + HMAC-SHA256 signature to query params."""
    params["timestamp"] = int(time.time() * 1000)
    query_string = urlencode(params)
    signature = hmac.new(
        config.BINANCE_API_SECRET.encode(),
        query_string.encode(),
        hashlib.sha256,
    ).hexdigest()
    params["signature"] = signature
    return params


def _headers() -> dict:
    return {"X-MBX-APIKEY": config.BINANCE_API_KEY}


def _get(path: str, params: dict | None = None, signed: bool = False) -> Any:
    """Issue a GET against the Binance Futures Testnet."""
    url = f"{config.BINANCE_TESTNET_BASE}{path}"
    params = params or {}
    if signed:
        params = _sign(params)
    resp = requests.get(url, params=params, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── Market data ──────────────────────────────────────────────────────

def fetch_candles(
    symbol: str = config.SYMBOL,
    interval: str = config.TIMEFRAME,
    limit: int = config.CANDLE_LIMIT,
) -> pd.DataFrame:
    """Return a DataFrame of OHLCV candles."""
    raw = _get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close", "volume", "quote_vol",
                "taker_buy_base", "taker_buy_quote"):
        df[col] = df[col].astype(float)
    df["trades"] = df["trades"].astype(int)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
    return df


# ── Technical Indicators ─────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute ALL technical indicators and return enriched DataFrame."""
    df = df.copy()

    # ── EMA ───────────────────────────────────────────────────────
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    # ── RSI 14 ────────────────────────────────────────────────────
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))

    # ── Stochastic RSI ────────────────────────────────────────────
    rsi = df["rsi14"]
    rsi_min = rsi.rolling(14).min()
    rsi_max = rsi.rolling(14).max()
    stoch_rsi = (rsi - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
    df["stoch_rsi_k"] = stoch_rsi.rolling(3).mean() * 100
    df["stoch_rsi_d"] = df["stoch_rsi_k"].rolling(3).mean()

    # ── ATR 14 ────────────────────────────────────────────────────
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.ewm(span=14, adjust=False).mean()

    # ── VWAP (cumulative intraday) ────────────────────────────────
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum()
    cum_tp_vol = (typical * df["volume"]).cumsum()
    df["vwap"] = cum_tp_vol / cum_vol.replace(0, np.nan)

    # ── Bollinger Bands (20, 2) ───────────────────────────────────
    sma20 = df["close"].rolling(20).mean()
    std20 = df["close"].rolling(20).std()
    df["bb_upper"] = sma20 + 2 * std20
    df["bb_middle"] = sma20
    df["bb_lower"] = sma20 - 2 * std20
    df["bb_width"] = ((df["bb_upper"] - df["bb_lower"]) / df["bb_middle"] * 100)

    # ── MACD (12, 26, 9) ─────────────────────────────────────────
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # ── Volume Moving Average (20) ────────────────────────────────
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"].replace(0, np.nan)

    # ── Buy/Sell Volume Ratio ─────────────────────────────────────
    df["buy_vol_pct"] = (df["taker_buy_base"] / df["volume"].replace(0, np.nan)) * 100

    return df


def extract_indicators(df: pd.DataFrame) -> dict:
    """Extract latest indicator values as a clean dict."""
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest

    return {
        "ema9": round(float(latest["ema9"]), 2),
        "ema20": round(float(latest["ema20"]), 2),
        "ema50": round(float(latest["ema50"]), 2),
        "rsi14": round(float(latest["rsi14"]), 2),
        "stoch_rsi_k": round(float(latest["stoch_rsi_k"]), 2) if pd.notna(latest["stoch_rsi_k"]) else None,
        "stoch_rsi_d": round(float(latest["stoch_rsi_d"]), 2) if pd.notna(latest["stoch_rsi_d"]) else None,
        "atr14": round(float(latest["atr14"]), 2),
        "vwap": round(float(latest["vwap"]), 2),
        "bb_upper": round(float(latest["bb_upper"]), 2),
        "bb_middle": round(float(latest["bb_middle"]), 2),
        "bb_lower": round(float(latest["bb_lower"]), 2),
        "bb_width": round(float(latest["bb_width"]), 4),
        "macd": round(float(latest["macd"]), 2),
        "macd_signal": round(float(latest["macd_signal"]), 2),
        "macd_hist": round(float(latest["macd_hist"]), 2),
        "macd_hist_prev": round(float(prev["macd_hist"]), 2),
        "vol_ratio": round(float(latest["vol_ratio"]), 2) if pd.notna(latest["vol_ratio"]) else None,
        "buy_vol_pct": round(float(latest["buy_vol_pct"]), 1) if pd.notna(latest["buy_vol_pct"]) else None,
        # Signals
        "ema_trend": "BULLISH" if latest["ema9"] > latest["ema20"] > latest["ema50"] else
                     "BEARISH" if latest["ema9"] < latest["ema20"] < latest["ema50"] else "MIXED",
        "price_vs_vwap": "ABOVE" if latest["close"] > latest["vwap"] else "BELOW",
        "bb_position": "UPPER" if latest["close"] > latest["bb_upper"] else
                       "LOWER" if latest["close"] < latest["bb_lower"] else "MIDDLE",
        "macd_cross": "BULLISH" if latest["macd_hist"] > 0 and prev["macd_hist"] <= 0 else
                      "BEARISH" if latest["macd_hist"] < 0 and prev["macd_hist"] >= 0 else "NONE",
    }


# ── Multi-Timeframe ─────────────────────────────────────────────────

def fetch_multi_timeframe(symbol: str = config.SYMBOL) -> dict:
    """Fetch 1m, 5m, 15m, 1h, 4h candles — full picture for Claude."""
    result = {}

    for tf, label, limit in [
        ("1m", "1m", 60),
        ("5m", "5m", 100),
        ("15m", "15m", 60),
        ("1h", "1h", 50),
        ("4h", "4h", 50),
    ]:
        try:
            df = fetch_candles(symbol=symbol, interval=tf, limit=limit)
            df = compute_indicators(df)
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else latest

            # Price action summary (last 5 candles)
            last5 = df.tail(5)
            green_count = (last5["close"] > last5["open"]).sum()
            avg_body = (last5["close"] - last5["open"]).abs().mean()

            result[label] = {
                "close": round(float(latest["close"]), 2),
                "ema9": round(float(latest["ema9"]), 2),
                "ema20": round(float(latest["ema20"]), 2),
                "ema50": round(float(latest["ema50"]), 2),
                "rsi14": round(float(latest["rsi14"]), 2),
                "atr14": round(float(latest["atr14"]), 2),
                "macd_hist": round(float(latest["macd_hist"]), 2),
                "macd_hist_prev": round(float(prev["macd_hist"]), 2),
                "stoch_rsi_k": round(float(latest["stoch_rsi_k"]), 2) if pd.notna(latest["stoch_rsi_k"]) else None,
                "bb_position": "UPPER" if latest["close"] > latest["bb_upper"] else
                               "LOWER" if latest["close"] < latest["bb_lower"] else "MIDDLE",
                "bb_width": round(float(latest["bb_width"]), 4),
                "vol_ratio": round(float(latest["vol_ratio"]), 2) if pd.notna(latest["vol_ratio"]) else None,
                "buy_vol_pct": round(float(latest["buy_vol_pct"]), 1) if pd.notna(latest["buy_vol_pct"]) else None,
                # Derived signals
                "trend": "BULLISH" if latest["ema9"] > latest["ema20"] > latest["ema50"] else
                         "BEARISH" if latest["ema9"] < latest["ema20"] < latest["ema50"] else "MIXED",
                "macd_cross": "BULLISH" if latest["macd_hist"] > 0 and prev["macd_hist"] <= 0 else
                              "BEARISH" if latest["macd_hist"] < 0 and prev["macd_hist"] >= 0 else "NONE",
                "momentum": "STRONG" if abs(latest["macd_hist"]) > latest["atr14"] * 0.1 else "WEAK",
                "last5_green": int(green_count),
                "last5_avg_body": round(float(avg_body), 2),
            }
        except Exception as exc:
            result[label] = {"error": str(exc)}

    return result


# ── Market Sentiment Data (Binance API) ──────────────────────────────

def fetch_funding_rate(symbol: str = config.SYMBOL) -> dict:
    """Fetch current funding rate."""
    try:
        data = _get("/fapi/v1/premiumIndex", {"symbol": symbol})
        return {
            "funding_rate": round(float(data.get("lastFundingRate", 0)) * 100, 4),
            "mark_price": round(float(data.get("markPrice", 0)), 2),
            "index_price": round(float(data.get("indexPrice", 0)), 2),
            "next_funding_time": data.get("nextFundingTime", 0),
        }
    except Exception:
        return {"funding_rate": 0, "mark_price": 0, "index_price": 0}


def fetch_open_interest(symbol: str = config.SYMBOL) -> dict:
    """Fetch open interest."""
    try:
        data = _get("/fapi/v1/openInterest", {"symbol": symbol})
        return {
            "open_interest": round(float(data.get("openInterest", 0)), 2),
        }
    except Exception:
        return {"open_interest": 0}


def fetch_long_short_ratio(symbol: str = config.SYMBOL) -> dict:
    """Fetch top trader long/short ratio."""
    try:
        data = _get("/futures/data/topLongShortAccountRatio", {
            "symbol": symbol, "period": "5m", "limit": 1,
        })
        if data:
            return {
                "long_account_pct": round(float(data[0].get("longAccount", 0.5)) * 100, 1),
                "short_account_pct": round(float(data[0].get("shortAccount", 0.5)) * 100, 1),
                "long_short_ratio": round(float(data[0].get("longShortRatio", 1)), 3),
            }
    except Exception:
        pass
    return {"long_account_pct": 50, "short_account_pct": 50, "long_short_ratio": 1.0}


def fetch_order_book_summary(symbol: str = config.SYMBOL, limit: int = 20) -> dict:
    """Fetch order book and compute bid/ask imbalance."""
    try:
        data = _get("/fapi/v1/depth", {"symbol": symbol, "limit": limit})
        bids = sum(float(b[1]) for b in data.get("bids", []))
        asks = sum(float(a[1]) for a in data.get("asks", []))
        total = bids + asks
        imbalance = ((bids - asks) / total * 100) if total > 0 else 0

        # Strongest bid/ask walls
        top_bid = max(data.get("bids", [[0, 0]]), key=lambda x: float(x[1]))
        top_ask = max(data.get("asks", [[0, 0]]), key=lambda x: float(x[1]))

        return {
            "bid_volume": round(bids, 3),
            "ask_volume": round(asks, 3),
            "imbalance_pct": round(imbalance, 1),
            "pressure": "BUY" if imbalance > 10 else "SELL" if imbalance < -10 else "NEUTRAL",
            "strongest_bid": {"price": float(top_bid[0]), "size": float(top_bid[1])},
            "strongest_ask": {"price": float(top_ask[0]), "size": float(top_ask[1])},
        }
    except Exception:
        return {"bid_volume": 0, "ask_volume": 0, "imbalance_pct": 0, "pressure": "NEUTRAL"}


def fetch_market_sentiment(symbol: str = config.SYMBOL) -> dict:
    """Aggregate all market sentiment data into one dict."""
    funding = fetch_funding_rate(symbol)
    oi = fetch_open_interest(symbol)
    ls_ratio = fetch_long_short_ratio(symbol)
    order_book = fetch_order_book_summary(symbol)

    return {
        "funding_rate": funding,
        "open_interest": oi,
        "long_short_ratio": ls_ratio,
        "order_book": order_book,
    }


# ── Account info ─────────────────────────────────────────────────────

def fetch_account_info() -> dict:
    """Return balance, positions, unrealised PnL for USDT futures."""
    data = _get("/fapi/v2/account", signed=True)

    # USDT balance
    usdt_balance = 0.0
    for asset in data.get("assets", []):
        if asset["asset"] == "USDT":
            usdt_balance = float(asset["walletBalance"])
            break

    # Open positions (non-zero)
    positions = []
    for p in data.get("positions", []):
        amt = float(p["positionAmt"])
        if amt != 0:
            positions.append({
                "symbol": p["symbol"],
                "side": "LONG" if amt > 0 else "SHORT",
                "size": abs(amt),
                "entry_price": float(p["entryPrice"]),
                "unrealized_pnl": float(p["unrealizedProfit"]),
                "leverage": int(p["leverage"]),
            })

    total_unrealized = sum(p["unrealized_pnl"] for p in positions)

    return {
        "usdt_balance": usdt_balance,
        "positions": positions,
        "unrealized_pnl": total_unrealized,
    }


def fetch_recent_trades(symbol: str = config.SYMBOL, limit: int = 5) -> list[dict]:
    """Return the last *limit* user trades."""
    raw = _get("/fapi/v1/userTrades", {"symbol": symbol, "limit": limit}, signed=True)
    trades = []
    for t in raw:
        trades.append({
            "time": t["time"],
            "side": t["side"],
            "price": float(t["price"]),
            "qty": float(t["qty"]),
            "realized_pnl": float(t["realizedPnl"]),
            "commission": float(t["commission"]),
        })
    return trades


def fetch_current_price(symbol: str = config.SYMBOL) -> float:
    """Return the latest mark price."""
    data = _get("/fapi/v1/ticker/price", {"symbol": symbol})
    return float(data["price"])
