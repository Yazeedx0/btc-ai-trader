"""
Execution engine — places market orders, stop-loss and take-profit on
Binance Futures Testnet.  Supports LONG, SHORT, and CLOSE.
"""

from __future__ import annotations

import time
import hmac
import hashlib
import math
from urllib.parse import urlencode
from typing import Any

import requests

import config


# ── HTTP helpers (signed POST) ───────────────────────────────────────

def _sign(params: dict) -> dict:
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


def _post(path: str, params: dict) -> Any:
    url = f"{config.BINANCE_TESTNET_BASE}{path}"
    params = _sign(params)
    resp = requests.post(url, params=params, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def _get(path: str, params: dict | None = None, signed: bool = False) -> Any:
    url = f"{config.BINANCE_TESTNET_BASE}{path}"
    params = params or {}
    if signed:
        params = _sign(params)
    resp = requests.get(url, params=params, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def _delete(path: str, params: dict) -> Any:
    url = f"{config.BINANCE_TESTNET_BASE}{path}"
    params = _sign(params)
    resp = requests.delete(url, params=params, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── Symbol info cache ────────────────────────────────────────────────

_symbol_info_cache: dict | None = None


def _get_symbol_info(symbol: str = config.SYMBOL) -> dict:
    global _symbol_info_cache
    if _symbol_info_cache is None:
        data = _get("/fapi/v1/exchangeInfo")
        for s in data["symbols"]:
            if s["symbol"] == symbol:
                _symbol_info_cache = s
                break
    return _symbol_info_cache or {}


def _get_quantity_precision(symbol: str = config.SYMBOL) -> int:
    info = _get_symbol_info(symbol)
    return int(info.get("quantityPrecision", 3))


def _get_price_precision(symbol: str = config.SYMBOL) -> int:
    info = _get_symbol_info(symbol)
    return int(info.get("pricePrecision", 2))


def _round_qty(qty: float, symbol: str = config.SYMBOL) -> float:
    prec = _get_quantity_precision(symbol)
    factor = 10 ** prec
    return math.floor(qty * factor) / factor


def _round_price(price: float, symbol: str = config.SYMBOL) -> float:
    prec = _get_price_precision(symbol)
    return round(price, prec)


# ── Leverage setter ──────────────────────────────────────────────────

def set_leverage(leverage: int, symbol: str = config.SYMBOL) -> dict:
    return _post("/fapi/v1/leverage", {
        "symbol": symbol,
        "leverage": leverage,
    })


# ── Order placement ──────────────────────────────────────────────────

def place_market_order(
    side: str,
    quantity: float,
    symbol: str = config.SYMBOL,
) -> dict:
    """Place a MARKET order.  side = 'BUY' or 'SELL'. Auto-retries with smaller size."""
    quantity = _round_qty(quantity, symbol)
    for attempt in range(3):
        try:
            return _post("/fapi/v1/order", {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": quantity,
            })
        except Exception as exc:
            if attempt < 2 and "400" in str(exc):
                quantity = _round_qty(quantity * 0.5, symbol)
                print(f"[EXEC] Order rejected, retrying with smaller qty={quantity}")
            else:
                raise


def place_stop_loss(
    side: str,
    stop_price: float,
    quantity: float,
    symbol: str = config.SYMBOL,
) -> dict:
    """Place a STOP_MARKET order as stop-loss."""
    quantity = _round_qty(quantity, symbol)
    stop_price = _round_price(stop_price, symbol)
    return _post("/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "STOP_MARKET",
        "stopPrice": stop_price,
        "quantity": quantity,
        "closePosition": "false",
        "workingType": "MARK_PRICE",
    })


def place_take_profit(
    side: str,
    stop_price: float,
    quantity: float,
    symbol: str = config.SYMBOL,
) -> dict:
    """Place a TAKE_PROFIT_MARKET order."""
    quantity = _round_qty(quantity, symbol)
    stop_price = _round_price(stop_price, symbol)
    return _post("/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "TAKE_PROFIT_MARKET",
        "stopPrice": stop_price,
        "quantity": quantity,
        "closePosition": "false",
        "workingType": "MARK_PRICE",
    })


def cancel_all_open_orders(symbol: str = config.SYMBOL) -> Any:
    """Cancel every open order for the symbol."""
    return _delete("/fapi/v1/allOpenOrders", {"symbol": symbol})


# ── High-level execution ────────────────────────────────────────────

def execute_open(
    decision: dict,
    balance: float,
    current_price: float,
) -> dict:
    """
    Open a new position (BUY = LONG, SELL = SHORT).
    Returns a summary dict with entry details.
    """
    action = decision["action"]  # BUY or SELL
    leverage = int(decision["leverage"])
    size_pct = float(decision["position_size_percent"])
    sl_price = float(decision["stop_loss"])
    tp_price = float(decision["take_profit"])

    # Set leverage first
    set_leverage(leverage)

    # Calculate notional & quantity
    notional = balance * (size_pct / 100) * leverage
    quantity = notional / current_price

    entry_side = "BUY" if action == "BUY" else "SELL"
    close_side = "SELL" if action == "BUY" else "BUY"

    # 1. Market entry
    entry_resp = place_market_order(entry_side, quantity)
    entry_price = float(entry_resp.get("avgPrice", current_price))

    # 2. Stop-loss
    try:
        place_stop_loss(close_side, sl_price, quantity)
    except Exception as exc:
        print(f"[EXEC] Warning: failed to place SL — {exc}")

    # 3. Take-profit
    try:
        place_take_profit(close_side, tp_price, quantity)
    except Exception as exc:
        print(f"[EXEC] Warning: failed to place TP — {exc}")

    return {
        "entry_price": entry_price,
        "quantity": _round_qty(quantity),
        "side": entry_side,
        "leverage": leverage,
        "stop_loss": sl_price,
        "take_profit": tp_price,
    }


def execute_close(symbol: str = config.SYMBOL) -> dict | None:
    """
    Close the current open position (if any) using a market order and
    cancel remaining SL/TP orders.
    """
    # Fetch current position
    acct = _get("/fapi/v2/account", signed=True)
    for p in acct.get("positions", []):
        if p["symbol"] == symbol:
            amt = float(p["positionAmt"])
            if amt == 0:
                return None
            close_side = "SELL" if amt > 0 else "BUY"
            qty = abs(amt)

            # Cancel pending SL/TP
            try:
                cancel_all_open_orders(symbol)
            except Exception:
                pass

            order_resp = place_market_order(close_side, qty, symbol)
            return {
                "close_price": float(order_resp.get("avgPrice", 0)),
                "quantity": qty,
                "side": close_side,
            }
    return None
