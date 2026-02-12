#!/usr/bin/env python3
"""
main.py — Smart trading loop with:
  - Full analysis every 5m candle close
  - Quick exit checks every 1m candle close (when in position)
  - Gemini manages all exits (no SL/TP on exchange)
  - Pyramiding: Gemini can ADD to winning positions
  - Trailing stop: Gemini tracks pnl_vs_atr and decides when to exit
"""

from __future__ import annotations

import signal
import time
import traceback
from datetime import datetime, timezone

import config
import data
import gemini_client
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


def get_position_info(account: dict, atr: float) -> dict | None:
    """Build position info dict for Claude, including pnl_vs_atr."""
    positions = account["positions"]
    if not positions:
        return None
    p = positions[0]
    entry = p["entry_price"]
    current = data.fetch_current_price()
    size = p["size"]
    side = p["side"]

    # Calculate PnL in price terms
    if side == "LONG":
        price_pnl = current - entry
    else:
        price_pnl = entry - current

    pnl_vs_atr = price_pnl / atr if atr > 0 else 0

    return {
        "side": side,
        "entry_price": entry,
        "current_price": current,
        "size": size,
        "leverage": p["leverage"],
        "unrealized_pnl": p["unrealized_pnl"],
        "pnl_vs_atr": round(pnl_vs_atr, 2),
        "price_change": round(price_pnl, 2),
    }


def fetch_quick_indicators() -> tuple[dict, dict]:
    """Fetch 1m and 5m indicators for quick check (lighter than full TF fetch)."""
    df_1m = data.fetch_candles(interval="1m", limit=60)
    df_1m = data.compute_indicators(df_1m)
    ind_1m = data.extract_indicators(df_1m)

    df_5m = data.fetch_candles(interval="5m", limit=60)
    df_5m = data.compute_indicators(df_5m)
    ind_5m = data.extract_indicators(df_5m)

    return ind_1m, ind_5m


# ── Track pyramid adds per position ─────────────────────────────────
_pyramid_count = 0


# ── Main loop ────────────────────────────────────────────────────────

def main() -> None:
    global _pyramid_count

    print_bar()
    print("  Gemini × Binance Futures — Smart Trailing Stop System")
    print(f"  Symbol : {config.SYMBOL}  |  Cycle : every {config.TIMEFRAME} candle")
    print(f"  Mode   : Gemini manages ALL exits (no SL/TP on exchange)")
    print(f"  AI     : Pro @5m (deep analysis) | Flash @1m (quick checks)")
    print(f"  Data   : 1m, 5m, 15m, 1h, 4h — full market picture")
    print_bar()

    # Validate env vars
    if not config.BINANCE_API_KEY or not config.BINANCE_API_SECRET:
        print("[ERROR] BINANCE_API_KEY / BINANCE_API_SECRET not set. Exiting.")
        return
    if not config.GEMINI_API_KEY:
        print("[ERROR] GEMINI_API_KEY not set. Exiting.")
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
    last_atr = 100.0  # fallback ATR

    while True:
        try:
            # ────────────────────────────────────────────────────────
            # WAIT LOOP: Check for 5m candle close OR 1m quick check
            # ────────────────────────────────────────────────────────
            heartbeat_ts = time.time()

            while True:
                time.sleep(0.1)

                # ── 5m candle closed → FULL ANALYSIS ─────────────────
                if ws_stream.is_candle_closed():
                    ws_stream.ack_candle_close()
                    # Also ack any pending 1m
                    if ws_stream.is_1m_candle_closed():
                        ws_stream.ack_1m_candle_close()
                    break  # → go to full analysis below

                # ── 1m candle closed + we have a position → QUICK CHECK
                if ws_stream.is_1m_candle_closed():
                    ws_stream.ack_1m_candle_close()

                    account_check = data.fetch_account_info()
                    if account_check["positions"]:
                        try:
                            ind_1m, ind_5m = fetch_quick_indicators()
                            pos_info = get_position_info(account_check, last_atr)

                            if pos_info:
                                pva = pos_info["pnl_vs_atr"]
                                pnl = pos_info["unrealized_pnl"]
                                side = pos_info["side"]

                                print(
                                    f"  [{ts_now()}] 🔍 1m CHECK: "
                                    f"{side} PnL=${pnl:+.2f} "
                                    f"({pva:+.1f}x ATR)  "
                                    f"1m_RSI={ind_1m['rsi14']}"
                                )

                                # Ask Gemini Flash for quick check
                                qc = gemini_client.get_quick_check(
                                    pos_info, ind_1m, ind_5m
                                )

                                if qc and qc["action"] == "CLOSE":
                                    print(
                                        f"  [{ts_now()}] ⚡ QUICK CLOSE: "
                                        f"{qc.get('comment', '')}"
                                    )
                                    balance_before = account_check["usdt_balance"]
                                    close_result = execution.execute_close()
                                    if close_result:
                                        new_acc = data.fetch_account_info()
                                        pnl_realized = new_acc["usdt_balance"] - balance_before
                                        risk_mgr.record_trade_result(pnl_realized)
                                        gemini_client.record_decision(
                                            {"action": "CLOSE", "confidence": qc["confidence"],
                                             "comment": f"[1m QUICK] {qc.get('comment', '')}",
                                             "leverage": 0, "position_size_percent": 0,
                                             "stop_loss": 0, "take_profit": 0},
                                            pnl=pnl_realized,
                                        )
                                        logger.log_trade(
                                            {"action": "CLOSE", "confidence": qc["confidence"],
                                             "comment": f"[1m QUICK] {qc.get('comment', '')}"},
                                            close_price=close_result["close_price"],
                                            position_size=close_result["quantity"],
                                            pnl=pnl_realized,
                                            equity=new_acc["usdt_balance"],
                                        )
                                        _pyramid_count = 0
                                        print(
                                            f"  [{ts_now()}] ⚡ CLOSED @ "
                                            f"${close_result['close_price']:.2f}  "
                                            f"PnL: {pnl_realized:+.2f}"
                                        )
                                else:
                                    if qc:
                                        print(
                                            f"  [{ts_now()}] ✓ HOLD: "
                                            f"{qc.get('comment', 'holding')[:60]}"
                                        )
                        except Exception as e:
                            print(f"  [{ts_now()}] [QUICK] Error: {e}")

                # Heartbeat every 15 seconds
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

            # ════════════════════════════════════════════════════════
            # FULL 5m ANALYSIS CYCLE
            # ════════════════════════════════════════════════════════
            cycle_count += 1
            print_bar()
            print(f"[{ts_now()}] ⚡ 5m CANDLE CLOSED — cycle #{cycle_count}")
            print_bar()

            # ── 1. Fetch ALL timeframes ──────────────────────────────
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
            last_atr = indicators.get("atr14", 100.0)

            print(
                f"[DATA] 5m: EMA9={indicators['ema9']:.2f}  "
                f"Trend={indicators['ema_trend']}  "
                f"RSI={indicators['rsi14']}  "
                f"ATR={last_atr:.2f}  "
                f"MACD_cross={indicators['macd_cross']}  "
                f"BB={indicators['bb_position']}"
            )

            # ── 3. Market sentiment + live WS data ───────────────────
            print("[DATA] Fetching market sentiment + live WS data …")
            market_sentiment = None
            try:
                market_sentiment = data.fetch_market_sentiment()
                ws_book = ws_stream.get_realtime_book()
                if ws_book["bid_volume"] > 0:
                    market_sentiment["order_book"] = ws_book
                market_sentiment["realtime_flow"] = ws_stream.get_realtime_flow()
                flow = market_sentiment["realtime_flow"]
                print(
                    f"[DATA] Funding={market_sentiment['funding_rate']['funding_rate']}%  "
                    f"Book={ws_book['pressure']}  "
                    f"Flow={flow['flow']}({flow['buy_pct_10s']:.0f}%buy)"
                )
            except Exception as e:
                print(f"[DATA] Market sentiment failed: {e}")

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

            # Check if we have a position — add info for Claude
            pos_info = get_position_info(account, last_atr) if positions else None
            if pos_info:
                print(
                    f"[DATA] Position: {pos_info['side']} @ ${pos_info['entry_price']:.2f}  "
                    f"PnL=${pos_info['unrealized_pnl']:+.2f}  "
                    f"({pos_info['pnl_vs_atr']:+.1f}x ATR)  "
                    f"Pyramids: {_pyramid_count}/{config.MAX_PYRAMID_ADDS}"
                )

            recent_trades = data.fetch_recent_trades()

            # ── 5. Ask Gemini Pro (FULL analysis) ────────────────────
            print("[GEMINI] Requesting decision (Pro — all TFs provided) …")

            # Inject position info into account for the payload
            if pos_info:
                account["open_position"] = pos_info
                account["pyramid_count"] = _pyramid_count
                account["max_pyramids"] = config.MAX_PYRAMID_ADDS

            decision = gemini_client.get_decision(
                df, account, recent_trades,
                indicators=indicators,
                market_sentiment=market_sentiment,
                multi_tf=multi_tf,
            )

            if decision is None:
                print("[GEMINI] No valid decision. Skipping.")
                continue

            action = decision["action"]
            print(f"[GEMINI] → {action} "
                  f"(conf={decision['confidence']}, "
                  f"lev={decision.get('leverage', 0)}x, "
                  f"size={decision.get('position_size_percent', 0)}%)")
            print(f"[GEMINI] Direction: {decision.get('market_direction', '?')}  "
                  f"TF used: {decision.get('timeframe_used', '?')}")
            print(f"[GEMINI] Comment: {decision.get('comment', '')}")

            # ── 6. Risk validation ───────────────────────────────────
            if action in ("BUY", "SELL"):
                allowed, reason = risk_mgr.validate(decision, balance, positions)
                print(f"[RISK]  {reason}")
                if not allowed:
                    logger.log_trade(decision, equity=balance)
                    continue

            # ── 7. Execute ───────────────────────────────────────────
            if action == "HOLD":
                print("[EXEC] HOLD — nothing to do.")
                logger.log_trade(decision, equity=balance)
                gemini_client.record_decision(decision)

            elif action == "CLOSE":
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
                    gemini_client.record_decision(decision, pnl=pnl)
                    logger.log_trade(
                        decision,
                        close_price=close_result["close_price"],
                        position_size=close_result["quantity"],
                        pnl=pnl,
                        equity=new_account["usdt_balance"],
                    )
                    _pyramid_count = 0
                    print(f"[EXEC] Realised PnL: {pnl:+.2f} USDT")
                else:
                    print("[EXEC] No position to close.")
                    logger.log_trade(decision, equity=balance)
                    gemini_client.record_decision(decision)

            elif action == "ADD":
                if not positions:
                    print("[EXEC] ADD — but no open position. Skipping.")
                elif _pyramid_count >= config.MAX_PYRAMID_ADDS:
                    print(f"[EXEC] ADD — max pyramids ({config.MAX_PYRAMID_ADDS}) reached.")
                else:
                    current_price = data.fetch_current_price()
                    print(f"[EXEC] ADDING to position @ ~{current_price:.2f} …")
                    try:
                        result = execution.execute_add(decision, balance, current_price)
                        _pyramid_count += 1
                        print(
                            f"[EXEC] Added: Entry={result['entry_price']:.2f}  "
                            f"Qty={result['quantity']}  "
                            f"(pyramid #{_pyramid_count})"
                        )
                        gemini_client.record_decision(decision)
                        logger.log_trade(
                            decision,
                            entry_price=result["entry_price"],
                            position_size=result["quantity"],
                            equity=balance,
                        )
                    except Exception as e:
                        print(f"[EXEC] ADD failed: {e}")

            elif action in ("BUY", "SELL"):
                current_price = data.fetch_current_price()
                print(f"[EXEC] Opening {action} @ ~{current_price:.2f} "
                      f"(Gemini manages exit — no SL/TP orders)")
                result = execution.execute_open(decision, balance, current_price)
                _pyramid_count = 0
                print(
                    f"[EXEC] Entry={result['entry_price']:.2f}  "
                    f"Qty={result['quantity']}  "
                    f"Leverage={result['leverage']}x  "
                    f"Mental SL={result['stop_loss']:.2f}  "
                    f"Mental TP={result['take_profit']:.2f}"
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
                gemini_client.record_decision(
                    decision,
                    entry_price=result["entry_price"],
                )

        except SystemExit:
            break
        except Exception:
            print(f"[ERROR] Unhandled exception:\n{traceback.format_exc()}")
            print("[ERROR] Sleeping 10s before retry …")
            time.sleep(10)


if __name__ == "__main__":
    main()
