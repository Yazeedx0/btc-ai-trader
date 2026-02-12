from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any

import websocket  # websocket-client

import config

# ── Binance Futures Testnet WS base ─────────────────────────────────
WS_BASE = "wss://stream.binancefuture.com/ws"

# ── Shared real-time state (thread-safe reads via GIL for simple types) ──
_lock = threading.Lock()

_state: dict[str, Any] = {
    # Price
    "price": 0.0,
    "price_ts": 0,

    # Latest kline
    "kline": {},
    "kline_5m_closed": False,
    "kline_5m_close_ack": True,
    "kline_1m_closed": False,
    "kline_1m_close_ack": True,

    # Order book
    "book_bids_vol": 0.0,
    "book_asks_vol": 0.0,
    "book_imbalance": 0.0,       # % (-100 to +100)
    "book_pressure": "NEUTRAL",
    "book_best_bid": 0.0,
    "book_best_ask": 0.0,

    # Aggregated trades (rolling 10s window)
    "agg_buy_vol": 0.0,
    "agg_sell_vol": 0.0,
    "agg_trade_count": 0,

    # Connection status
    "connected": False,
    "last_msg_ts": 0,
}

# Rolling window for aggregated trades (keep last 10 seconds)
_agg_trades: deque = deque(maxlen=500)


# ── Public API ───────────────────────────────────────────────────────

def get_state() -> dict:
    """Return a snapshot of the current real-time state."""
    with _lock:
        return dict(_state)


def get_realtime_price() -> float:
    """Return the latest real-time price."""
    return _state["price"]


def is_candle_closed() -> bool:
    """Check if a 5m candle just closed (unacknowledged)."""
    return _state["kline_5m_closed"] and not _state["kline_5m_close_ack"]


def ack_candle_close() -> None:
    """Acknowledge the 5m candle close so we don't re-trigger."""
    with _lock:
        _state["kline_5m_close_ack"] = True


def is_1m_candle_closed() -> bool:
    """Check if a 1m candle just closed (for quick exit checks)."""
    return _state["kline_1m_closed"] and not _state["kline_1m_close_ack"]


def ack_1m_candle_close() -> None:
    """Acknowledge the 1m candle close."""
    with _lock:
        _state["kline_1m_close_ack"] = True


def get_realtime_book() -> dict:
    """Return real-time order book summary."""
    with _lock:
        return {
            "bid_volume": _state["book_bids_vol"],
            "ask_volume": _state["book_asks_vol"],
            "imbalance_pct": round(_state["book_imbalance"], 1),
            "pressure": _state["book_pressure"],
            "best_bid": _state["book_best_bid"],
            "best_ask": _state["book_best_ask"],
        }


def get_realtime_flow() -> dict:
    """Return real-time buy/sell volume flow from agg trades (last 10s)."""
    now = time.time() * 1000
    cutoff = now - 10_000  # 10 seconds
    buy_vol = 0.0
    sell_vol = 0.0
    count = 0
    for trade in list(_agg_trades):
        if trade["ts"] >= cutoff:
            if trade["is_buy"]:
                buy_vol += trade["qty"]
            else:
                sell_vol += trade["qty"]
            count += 1
    total = buy_vol + sell_vol
    return {
        "buy_vol_10s": round(buy_vol, 4),
        "sell_vol_10s": round(sell_vol, 4),
        "buy_pct_10s": round(buy_vol / total * 100, 1) if total > 0 else 50.0,
        "trade_count_10s": count,
        "flow": "BUY" if buy_vol > sell_vol * 1.2 else
                "SELL" if sell_vol > buy_vol * 1.2 else "NEUTRAL",
    }


# ── WebSocket message handler ───────────────────────────────────────

def _on_message(ws, raw_msg: str) -> None:
    """Handle incoming WS messages."""
    try:
        msg = json.loads(raw_msg)
    except json.JSONDecodeError:
        return

    event = msg.get("e", "")

    with _lock:
        _state["last_msg_ts"] = int(time.time() * 1000)

        # ── Kline (candlestick) ──────────────────────────────────
        if event == "kline":
            k = msg["k"]
            _state["kline"] = {
                "open_time": k["t"],
                "open": float(k["o"]),
                "high": float(k["h"]),
                "low": float(k["l"]),
                "close": float(k["c"]),
                "volume": float(k["v"]),
                "is_closed": k["x"],
            }
            _state["price"] = float(k["c"])
            _state["price_ts"] = int(time.time() * 1000)

            # Signal candle close by interval
            interval = k.get("i", "")
            if k["x"]:  # candle is closed
                if interval == "5m":
                    _state["kline_5m_closed"] = True
                    _state["kline_5m_close_ack"] = False
                elif interval == "1m":
                    _state["kline_1m_closed"] = True
                    _state["kline_1m_close_ack"] = False

        # ── Aggregated trade ─────────────────────────────────────
        elif event == "aggTrade":
            price = float(msg["p"])
            qty = float(msg["q"])
            is_buyer_maker = msg["m"]  # True = sell (maker is buyer)
            _state["price"] = price
            _state["price_ts"] = int(time.time() * 1000)
            _agg_trades.append({
                "ts": int(time.time() * 1000),
                "price": price,
                "qty": qty,
                "is_buy": not is_buyer_maker,
            })

        # ── Order book depth ─────────────────────────────────────
        elif event == "depthUpdate" or "bids" in msg:
            bids = msg.get("b", msg.get("bids", []))
            asks = msg.get("a", msg.get("asks", []))
            if bids and asks:
                bid_vol = sum(float(b[1]) for b in bids)
                ask_vol = sum(float(a[1]) for a in asks)
                total = bid_vol + ask_vol
                imbalance = ((bid_vol - ask_vol) / total * 100) if total > 0 else 0

                _state["book_bids_vol"] = round(bid_vol, 3)
                _state["book_asks_vol"] = round(ask_vol, 3)
                _state["book_imbalance"] = imbalance
                _state["book_pressure"] = (
                    "BUY" if imbalance > 10 else
                    "SELL" if imbalance < -10 else "NEUTRAL"
                )
                if bids:
                    _state["book_best_bid"] = float(bids[0][0])
                if asks:
                    _state["book_best_ask"] = float(asks[0][0])


def _on_open(ws) -> None:
    """Subscribe to streams on connection."""
    symbol = config.SYMBOL.lower()
    streams = [
        f"{symbol}@kline_5m",
        f"{symbol}@kline_1m",
        f"{symbol}@aggTrade",
        f"{symbol}@depth20@100ms",
    ]
    subscribe_msg = {
        "method": "SUBSCRIBE",
        "params": streams,
        "id": 1,
    }
    ws.send(json.dumps(subscribe_msg))
    with _lock:
        _state["connected"] = True
    print("[WS] Connected & subscribed:", streams)


def _on_close(ws, close_status_code, close_msg) -> None:
    with _lock:
        _state["connected"] = False
    print(f"[WS] Disconnected: {close_status_code} {close_msg}")


def _on_error(ws, error) -> None:
    print(f"[WS] Error: {error}")


# ── Background thread ────────────────────────────────────────────────

_ws_thread: threading.Thread | None = None
_ws_instance: websocket.WebSocketApp | None = None


def start() -> None:
    """Start the WebSocket feed in a background daemon thread."""
    global _ws_thread, _ws_instance

    if _ws_thread is not None and _ws_thread.is_alive():
        print("[WS] Already running.")
        return

    _ws_instance = websocket.WebSocketApp(
        WS_BASE,
        on_open=_on_open,
        on_message=_on_message,
        on_close=_on_close,
        on_error=_on_error,
    )

    def _run():
        while True:
            try:
                _ws_instance.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as exc:
                print(f"[WS] Exception in WS thread: {exc}")
            # Auto-reconnect after 3 seconds
            with _lock:
                _state["connected"] = False
            print("[WS] Reconnecting in 3s …")
            time.sleep(3)

    _ws_thread = threading.Thread(target=_run, daemon=True, name="ws-stream")
    _ws_thread.start()
    print("[WS] Background stream started.")

    # Wait briefly for first message
    time.sleep(2)


def stop() -> None:
    """Close the WebSocket connection."""
    global _ws_instance
    if _ws_instance:
        _ws_instance.close()
        _ws_instance = None
    print("[WS] Stopped.")


def is_connected() -> bool:
    return _state.get("connected", False)
