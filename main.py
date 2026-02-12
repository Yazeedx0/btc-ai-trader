#!/usr/bin/env python3
"""
main.py — Real-time event loop for the Claude-powered Binance Futures paper trader.

Uses WebSocket streams for instant price, order book, and candle close
detection.  Falls back to REST for historical candles & indicators.
Claude receives ALL timeframes (1m-4h) and decides the market direction.
"""

from __future__ import annotations

import signal
import time
import json
import traceback
from datetime import datetime, timezone

import config
import data
import claude_client
import risk
import execution
import logger
import ws_stream


# ── Helpers ──────────────────────────────────────────────────────────

def ts_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def print_bar() -> None:
    print("=" * 72)


def close_all_positions() -> None:
    """Close any open positions before exit."""
    try:
        account = data.fetch_account_info()
        for pos in account["positions"]:
            amt = float(pos.get("positionAmt", 0))
            if amt != 0:
                print(f"[EXIT] Closing position: {pos['symbol']} amt={amt}")
                result = execution.execute_close(symbol=pos["symbol"])
                if result:
                    print(f"[EXIT] Closed {pos['symbol']} @ {result['close_price']:.2f}")
                else:
                    print(f"[EXIT] Failed to close {pos['symbol']}")
        print("[EXIT] All positions closed.")
    except Exception as e:
        print(f"[EXIT] Error closing positions: {e}")


# ── Main loop ────────────────────────────────────────────────────────

def main() -> None:
    print_bar()
    print("  Claude × Binance Futures — Multi-Timeframe Paper Trader")
    print(f"  Symbol : {config.SYMBOL}  |  Cycle : every {config.TIMEFRAME} candle")
    print(f"  Mode   : WebSocket real-time  |  Claude chooses the timeframe")
    print(f"  Data   : 1m, 5m, 15m, 1h, 4h — full market picture")
    print_bar()

    # Validate env vars
    if not config.BINANCE_API_KEY or not config.BINANCE_API_SECRET:
        print("[ERROR] BINANCE_API_KEY / BINANCE_API_SECRET not set. Exiting.")
        return
    if not config.CLAUDE_API_KEY:
        print("[ERROR] CLAUDE_API_KEY not set. Exiting.")
        return

    # Fetch starting balance once
    account = data.fetch_account_info()
    starting_balance = account["usdt_balance"]
    print(f"[INIT] Starting balance: {starting_balance:.2f} USDT")

    risk_mgr = risk.RiskManager(starting_balance)

    # ── Ctrl+C handler — close all positions on exit ─────────────
    _shutting_down = False

    def _shutdown_handler(sig, frame):
        nonlocal _shutting_down
        if _shutting_down:
            print("\n[EXIT] Force exit.")
            raise SystemExit(1)
        _shutting_down = True
        print("\n" + "=" * 72)
        print("[EXIT] Ctrl+C detected — closing all positions …")
        print("=" * 72)
        close_all_positions()
        ws_stream.stop()
        print("[EXIT] Goodbye!")
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    # ── Start WebSocket stream ───────────────────────────────────
    print("[INIT] Starting real-time WebSocket feed …")
    ws_stream.start()

    if not ws_stream.is_connected():
        print("[WARN] WebSocket not connected yet, waiting …")
        for _ in range(10):
            time.sleep(1)
            if ws_stream.is_connected():
                break
        if not ws_stream.is_connected():
            print("[WARN] WS still not connected — will continue with REST fallback")

    print("[INIT] Real-time feed active ✓")
    print_bar()

    cycle_count = 0

    while True:
        try:
            # ── Wait for 5m candle close (real-time detection) ───────
            rt_state = ws_stream.get_state()

            # Show heartbeat every 15s while waiting
            heartbeat_ts = time.time()
            while not ws_stream.is_candle_closed():
                time.sleep(0.1)  # 100ms polling — near instant reaction

                if time.time() - heartbeat_ts > 15:
                    rt = ws_stream.get_state()
                    flow = ws_stream.get_realtime_flow()
                    book = ws_stream.get_realtime_book()
                    print(
                        f"  [{ts_now()}] ⚡ ${rt['price']:.2f}  "
                        f"Book={book['pressure']}({book['imbalance_pct']:+.0f}%)  "
                        f"Flow={flow['flow']}({flow['buy_pct_10s']:.0f}%buy)  "
                        f"WS={'✓' if rt['connected'] else '✗'}"
                    )
                    heartbeat_ts = time.time()

            # ── 5m Candle closed! React immediately ──────────────────
            ws_stream.ack_candle_close()
            cycle_count += 1

            print_bar()
            print(f"[{ts_now()}] ⚡ 5m CANDLE CLOSED — cycle #{cycle_count}")
            print_bar()

            # ── 1. Fetch ALL timeframes (1m, 5m, 15m, 1h, 4h) ───────
            print("[DATA] Fetching ALL timeframes (1m, 5m, 15m, 1h, 4h) …")
            try:
                multi_tf = data.fetch_multi_timeframe()
                for tf_label, tf_data in multi_tf.items():
                    if "error" not in tf_data:
                        print(
                            f"[DATA]   {tf_label:>3s}: trend={tf_data['trend']:8s}  "
                            f"RSI={tf_data['rsi14']:5.1f}  "
                            f"MACD_cross={tf_data['macd_cross']:8s}  "
                            f"momentum={tf_data['momentum']:6s}  "
                            f"BB={tf_data['bb_position']}"
                        )
                    else:
                        print(f"[DATA]   {tf_label}: ERROR — {tf_data['error']}")
            except Exception as e:
                print(f"[DATA] Multi-TF failed: {e}")
                multi_tf = None

            # ── 2. Base 5m candles + indicators ──────────────────────
            print("[DATA] Fetching 5m candles + computing indicators …")
            df = data.fetch_candles()
            df = data.compute_indicators(df)
            indicators = data.extract_indicators(df)

            print(
                f"[DATA] 5m: EMA9={indicators['ema9']:.2f}  "
                f"Trend={indicators['ema_trend']}  "
                f"RSI={indicators['rsi14']}  "
                f"StochK={indicators.get('stoch_rsi_k', 'N/A')}  "
                f"MACD_cross={indicators['macd_cross']}  "
                f"BB={indicators['bb_position']}  "
                f"VolRatio={indicators.get('vol_ratio', 'N/A')}"
            )

            # ── 3. Market sentiment + live WS data ───────────────────
            print("[DATA] Fetching market sentiment + live WS data …")
            try:
                market_sentiment = data.fetch_market_sentiment()

                # Override order book with live WS order book (more current)
                ws_book = ws_stream.get_realtime_book()
                if ws_book["bid_volume"] > 0:
                    market_sentiment["order_book"] = ws_book

                # Add real-time trade flow
                market_sentiment["realtime_flow"] = ws_stream.get_realtime_flow()

                funding = market_sentiment["funding_rate"]["funding_rate"]
                oi = market_sentiment["open_interest"]["open_interest"]
                ls = market_sentiment["long_short_ratio"]["long_short_ratio"]
                flow = market_sentiment["realtime_flow"]
                print(
                    f"[DATA] Funding={funding}%  OI={oi}  "
                    f"L/S={ls}  Book={ws_book['pressure']}  "
                    f"Flow={flow['flow']}({flow['buy_pct_10s']:.0f}%buy, "
                    f"{flow['trade_count_10s']} trades/10s)"
                )
            except Exception as e:
                print(f"[DATA] Market sentiment failed: {e}")
                market_sentiment = None

            # ── 4. Account state ─────────────────────────────────────
            print("[DATA] Fetching account …")
            account = data.fetch_account_info()
            balance = account["usdt_balance"]
            positions = account["positions"]
            print(
                f"[DATA] Balance={balance:.2f}  "
                f"Positions={len(positions)}  "
                f"uPnL={account['unrealized_pnl']:.2f}"
            )

            recent_trades = data.fetch_recent_trades()

            # ── 5. Ask Claude (with ALL timeframes + sentiment) ──────
            print("[CLAUDE] Requesting decision (all TFs provided) …")
            decision = claude_client.get_decision(
                df, account, recent_trades,
                indicators=indicators,
                market_sentiment=market_sentiment,
                multi_tf=multi_tf,
            )

            if decision is None:
                print("[CLAUDE] No valid decision. Skipping.")
                continue

            print(f"[CLAUDE] → {decision['action']} "
                  f"(conf={decision['confidence']}, "
                  f"lev={decision['leverage']}x, "
                  f"size={decision['position_size_percent']}%)")
            print(f"[CLAUDE] Direction: {decision.get('market_direction', '?')}  "
                  f"TF used: {decision.get('timeframe_used', '?')}")
            print(f"[CLAUDE] Comment: {decision.get('comment', '')}")

            action = decision["action"]

            # ── 6. Risk validation ───────────────────────────────────
            allowed, reason = risk_mgr.validate(decision, balance, positions)
            print(f"[RISK]  {reason}")

            if not allowed:
                logger.log_trade(decision, equity=balance)
                continue

            # ── 7. Execute ───────────────────────────────────────────
            if action == "HOLD":
                print("[EXEC] HOLD — nothing to do.")
                logger.log_trade(decision, equity=balance)
                claude_client.record_decision(decision)
                continue

            if action == "CLOSE":
                print("[EXEC] Closing open position …")
                close_result = execution.execute_close()
                if close_result:
                    print(
                        f"[EXEC] Closed at {close_result['close_price']:.2f}  "
                        f"qty={close_result['quantity']}"
                    )
                    new_account = data.fetch_account_info()
                    pnl = new_account["usdt_balance"] - balance
                    risk_mgr.record_trade_result(pnl)
                    claude_client.record_decision(decision, pnl=pnl)
                    logger.log_trade(
                        decision,
                        close_price=close_result["close_price"],
                        position_size=close_result["quantity"],
                        pnl=pnl,
                        equity=new_account["usdt_balance"],
                    )
                    print(f"[EXEC] Realised PnL: {pnl:+.2f} USDT")
                else:
                    print("[EXEC] No position to close.")
                    logger.log_trade(decision, equity=balance)
                    claude_client.record_decision(decision)
                continue

            if action in ("BUY", "SELL"):
                current_price = data.fetch_current_price()
                print(f"[EXEC] Opening {action} @ ~{current_price:.2f} …")
                result = execution.execute_open(decision, balance, current_price)
                print(
                    f"[EXEC] Entry={result['entry_price']:.2f}  "
                    f"Qty={result['quantity']}  "
                    f"Leverage={result['leverage']}x  "
                    f"SL={result['stop_loss']:.2f}  TP={result['take_profit']:.2f}"
                )
                logger.log_trade(
                    decision,
                    entry_price=result["entry_price"],
                    position_size=result["quantity"],
                    leverage=result["leverage"],
                    stop_loss=result["stop_loss"],
                    take_profit=result["take_profit"],
                    equity=balance,
                )
                claude_client.record_decision(
                    decision,
                    entry_price=result["entry_price"],
                )
                continue

        except SystemExit:
            break
        except Exception:
            print(f"[ERROR] Unhandled exception:\n{traceback.format_exc()}")
            print("[ERROR] Sleeping 10s before retry …")
            time.sleep(10)


if __name__ == "__main__":
    main()
