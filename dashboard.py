#!/usr/bin/env python3
"""
Live dashboard — Real-time monitoring with WebSocket price feed,
technical indicators, market sentiment, order flow, and trade history.
"""

import os
import time
import csv
from datetime import datetime, timezone

import config
import data
import ws_stream


# ── ANSI colors ──────────────────────────────────────────────────────
G = "\033[92m"   # green
R = "\033[91m"   # red
Y = "\033[93m"   # yellow
C = "\033[96m"   # cyan
M = "\033[95m"   # magenta
W = "\033[97m"   # white/bold
D = "\033[90m"   # dim/gray
B = "\033[1m"    # bold
RST = "\033[0m"  # reset


def clear():
    os.system("clear")


def ts_now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def color_pnl(val: float) -> str:
    if val > 0:
        return f"{G}+{val:,.2f}{RST}"
    elif val < 0:
        return f"{R}{val:,.2f}{RST}"
    return f"{D}0.00{RST}"


def color_pct(val: float) -> str:
    if val > 0:
        return f"{G}+{val:.1f}%{RST}"
    elif val < 0:
        return f"{R}{val:.1f}%{RST}"
    return f"{D}0.0%{RST}"


def bar(value: float, max_val: float = 100, width: int = 20, fill_color: str = G) -> str:
    """Create a visual bar."""
    ratio = max(0, min(1, value / max_val)) if max_val > 0 else 0
    filled = int(ratio * width)
    empty = width - filled
    return f"{fill_color}{'█' * filled}{D}{'░' * empty}{RST}"


def pressure_bar(imbalance: float, width: int = 30) -> str:
    """Buy/Sell pressure bar centered at 0."""
    mid = width // 2
    if imbalance > 0:
        filled = int(min(1, imbalance / 50) * mid)
        return f"{D}{'░' * mid}{RST}{G}{'█' * filled}{'░' * (mid - filled)}{RST}"
    else:
        filled = int(min(1, abs(imbalance) / 50) * mid)
        return f"{R}{'░' * (mid - filled)}{'█' * filled}{RST}{D}{'░' * mid}{RST}"


def read_trade_log(limit: int = 12) -> list[dict]:
    if not os.path.exists(config.TRADE_LOG_FILE):
        return []
    with open(config.TRADE_LOG_FILE) as f:
        rows = list(csv.DictReader(f))
    return rows[-limit:]


def display():
    clear()
    W2 = 82

    # ── Header ───────────────────────────────────────────────────────
    print(f"{B}{C}{'═' * W2}{RST}")
    print(f"{B}{C}  ⚡ REAL-TIME PAPER TRADING DASHBOARD{RST}")
    print(f"{C}  {config.SYMBOL}  │  {ts_now()}  │  Refresh: 2s{RST}")
    print(f"{B}{C}{'═' * W2}{RST}")

    # ── WebSocket Status & Live Price ────────────────────────────────
    rt = ws_stream.get_state()
    ws_ok = rt["connected"]
    ws_icon = f"{G}● LIVE{RST}" if ws_ok else f"{R}● OFFLINE{RST}"
    price = rt["price"] if rt["price"] > 0 else 0

    try:
        if price == 0:
            price = data.fetch_current_price()
    except Exception:
        pass

    print(f"\n  {ws_icon}  │  {B}{W}BTC  ${price:,.2f}{RST}")

    # Spread from order book
    book = ws_stream.get_realtime_book()
    if book["best_bid"] > 0 and book["best_ask"] > 0:
        spread = book["best_ask"] - book["best_bid"]
        print(f"  Bid: {G}${book['best_bid']:,.2f}{RST}  │  Ask: {R}${book['best_ask']:,.2f}{RST}  │  Spread: ${spread:,.2f}")

    # ── Account ──────────────────────────────────────────────────────
    print(f"\n  {D}{'─' * (W2 - 4)}{RST}")
    try:
        account = data.fetch_account_info()
        balance = account["usdt_balance"]
        unrealized = account["unrealized_pnl"]
        equity = balance + unrealized
        positions = account["positions"]

        print(f"  {B}ACCOUNT{RST}")
        print(f"  Balance:     {W}${balance:>12,.2f}{RST}")
        print(f"  Unrealised:  {color_pnl(unrealized):>24s}")
        print(f"  Equity:      {B}${equity:>12,.2f}{RST}")

        # ── Open Positions ───────────────────────────────────────────
        print(f"\n  {D}{'─' * (W2 - 4)}{RST}")
        if positions:
            print(f"  {B}OPEN POSITIONS{RST}")
            for p in positions:
                side_c = G if p["side"] == "LONG" else R
                icon = "▲" if p["side"] == "LONG" else "▼"
                pnl_s = color_pnl(p["unrealized_pnl"])
                pnl_pct = (p["unrealized_pnl"] / (p["entry_price"] * p["size"])) * 100 if p["entry_price"] > 0 else 0
                print(
                    f"  {side_c}{icon} {p['side']:<5}{RST} "
                    f"{p['size']:.4f} BTC @ ${p['entry_price']:,.2f}  "
                    f"{p['leverage']}x  │  PnL: {pnl_s} ({color_pct(pnl_pct)})"
                )
        else:
            print(f"  {D}No open positions{RST}")
    except Exception as e:
        print(f"  {R}Account fetch failed: {e}{RST}")

    # ── Technical Indicators ─────────────────────────────────────────
    print(f"\n  {D}{'─' * (W2 - 4)}{RST}")
    print(f"  {B}INDICATORS{RST}")
    try:
        df = data.fetch_candles()
        df = data.compute_indicators(df)
        ind = data.extract_indicators(df)

        # Trend summary
        trend_c = G if ind["ema_trend"] == "BULLISH" else R if ind["ema_trend"] == "BEARISH" else Y
        print(f"  Trend: {trend_c}{B}{ind['ema_trend']}{RST}  │  "
              f"EMA9={ind['ema9']}  EMA20={ind['ema20']}  EMA50={ind['ema50']}")

        # RSI bar
        rsi = ind["rsi14"]
        rsi_c = R if rsi > 70 else G if rsi < 30 else W
        print(f"  RSI:   {rsi_c}{rsi:>5.1f}{RST}  {bar(rsi, 100, 25, rsi_c)}  "
              f"{'⚠ OVERBOUGHT' if rsi > 70 else '⚠ OVERSOLD' if rsi < 30 else ''}")

        # Stochastic RSI
        sk = ind.get("stoch_rsi_k")
        sd = ind.get("stoch_rsi_d")
        if sk is not None:
            sk_c = R if sk > 80 else G if sk < 20 else W
            cross = ""
            if sk is not None and sd is not None:
                cross = f"  {G}▲ K>D{RST}" if sk > sd else f"  {R}▼ K<D{RST}"
            print(f"  StRSI: {sk_c}K={sk:>5.1f}{RST} D={sd:>5.1f}{cross}")

        # MACD
        macd_c = G if ind["macd_hist"] > 0 else R
        cross_s = ""
        if ind["macd_cross"] == "BULLISH":
            cross_s = f"  {G}★ BULLISH CROSS{RST}"
        elif ind["macd_cross"] == "BEARISH":
            cross_s = f"  {R}★ BEARISH CROSS{RST}"
        print(f"  MACD:  {macd_c}H={ind['macd_hist']:>+7.2f}{RST}  "
              f"S={ind['macd_signal']:>+7.2f}{cross_s}")

        # Bollinger Bands
        bb_c = R if ind["bb_position"] == "UPPER" else G if ind["bb_position"] == "LOWER" else D
        print(f"  BB:    {bb_c}{ind['bb_position']}{RST}  "
              f"[{ind['bb_lower']:.0f} ─ {ind['bb_middle']:.0f} ─ {ind['bb_upper']:.0f}]  "
              f"Width={ind['bb_width']:.3f}%")

        # VWAP
        vwap_c = G if ind["price_vs_vwap"] == "ABOVE" else R
        print(f"  VWAP:  {vwap_c}{ind['price_vs_vwap']}{RST} @ ${ind['vwap']:,.2f}  │  "
              f"ATR: ${ind['atr14']:,.2f}")

        # Volume
        vr = ind.get("vol_ratio")
        bvp = ind.get("buy_vol_pct")
        if vr is not None:
            vr_c = Y if vr > 1.5 else G if vr > 1 else D
            vol_alert = f"  {Y}⚡ HIGH VOLUME{RST}" if vr > 2 else ""
            print(f"  Vol:   {vr_c}×{vr:.2f}{RST}  "
                  f"Buy={bvp:.0f}%{vol_alert}" if bvp else "")

    except Exception as e:
        print(f"  {R}Indicator fetch failed: {e}{RST}")

    # ── Market Sentiment ─────────────────────────────────────────────
    print(f"\n  {D}{'─' * (W2 - 4)}{RST}")
    print(f"  {B}MARKET SENTIMENT{RST}")
    try:
        sentiment = data.fetch_market_sentiment()

        # Funding rate
        fr = sentiment["funding_rate"]["funding_rate"]
        fr_c = G if fr < 0 else R if fr > 0.03 else W
        print(f"  Funding:   {fr_c}{fr:>+.4f}%{RST}  "
              f"{'📈 Longs paying' if fr > 0 else '📉 Shorts paying'}")

        # Open Interest
        oi = sentiment["open_interest"]["open_interest"]
        print(f"  OI:        {W}{oi:>12,.2f} BTC{RST}")

        # Long/Short Ratio
        ls = sentiment["long_short_ratio"]
        ls_c = G if ls["long_short_ratio"] > 1 else R
        long_bar = bar(ls["long_account_pct"], 100, 15, G)
        short_bar = bar(ls["short_account_pct"], 100, 15, R)
        print(f"  L/S Ratio: {ls_c}{ls['long_short_ratio']:.3f}{RST}  "
              f"│  L:{ls['long_account_pct']:.0f}% {long_bar}  "
              f"S:{ls['short_account_pct']:.0f}% {short_bar}")

        # Order Book (from WS)
        ws_book = ws_stream.get_realtime_book()
        if ws_book["bid_volume"] > 0:
            p_bar = pressure_bar(ws_book["imbalance_pct"])
            p_c = G if ws_book["pressure"] == "BUY" else R if ws_book["pressure"] == "SELL" else D
            print(f"  Book:      {p_c}{ws_book['pressure']}{RST} "
                  f"({ws_book['imbalance_pct']:+.1f}%)  "
                  f"SELL {p_bar} BUY")

        # Real-time trade flow
        flow = ws_stream.get_realtime_flow()
        flow_c = G if flow["flow"] == "BUY" else R if flow["flow"] == "SELL" else D
        print(f"  Flow(10s): {flow_c}{flow['flow']}{RST}  "
              f"Buy={flow['buy_pct_10s']:.0f}%  "
              f"({flow['trade_count_10s']} trades)  "
              f"Vol: {G}{flow['buy_vol_10s']:.3f}{RST}/{R}{flow['sell_vol_10s']:.3f}{RST}")

    except Exception as e:
        print(f"  {R}Sentiment fetch failed: {e}{RST}")

    # ── Multi-Timeframe ──────────────────────────────────────────────
    print(f"\n  {D}{'─' * (W2 - 4)}{RST}")
    print(f"  {B}HIGHER TIMEFRAMES{RST}")
    try:
        mtf = data.fetch_multi_timeframe()
        for tf_label, tf_data in mtf.items():
            if "error" not in tf_data:
                t_c = G if tf_data["trend"] == "BULLISH" else R
                m_c = Y if tf_data["momentum"] == "STRONG" else D
                print(
                    f"  {tf_label:>3}: {t_c}{tf_data['trend']:<8}{RST}  "
                    f"RSI={tf_data['rsi14']:>5.1f}  "
                    f"MACD_H={tf_data['macd_hist']:>+7.2f}  "
                    f"Mom={m_c}{tf_data['momentum']}{RST}"
                )
            else:
                print(f"  {tf_label:>3}: {R}Error{RST}")
    except Exception as e:
        print(f"  {R}MTF fetch failed: {e}{RST}")

    # ── Trade Log ────────────────────────────────────────────────────
    log_rows = read_trade_log(8)
    print(f"\n  {D}{'─' * (W2 - 4)}{RST}")
    print(f"  {B}RECENT DECISIONS{RST}")
    if log_rows:
        print(f"  {D}{'Time':<11} {'Act':<5} {'Entry':>10} {'PnL':>10} {'Equity':>11} {'Comment'}{RST}")
        for r in log_rows:
            ts = r.get("timestamp", "")
            # Extract just time part
            t_part = ts[11:19] if len(ts) > 19 else ts[:8]
            act = r.get("action", "?")
            act_c = G if act == "BUY" else R if act == "SELL" else Y if act == "CLOSE" else D
            entry = r.get("entry_price", "")
            pnl = r.get("pnl", "")
            eq = r.get("equity", "")
            comment = r.get("comment", "")[:35]

            entry_s = f"${float(entry):>9,.2f}" if entry else f"{'—':>10}"
            pnl_s = color_pnl(float(pnl)) if pnl else f"{'—':>10}"
            eq_s = f"${float(eq):>10,.2f}" if eq else f"{'—':>11}"

            print(f"  {D}{t_part}{RST} {act_c}{act:<5}{RST} {entry_s} {pnl_s:>22s} {eq_s} {D}{comment}{RST}")
    else:
        print(f"  {D}No trades yet — start main.py first{RST}")

    # ── Recent Binance Fills ─────────────────────────────────────────
    print(f"\n  {D}{'─' * (W2 - 4)}{RST}")
    print(f"  {B}LAST 5 FILLS{RST}")
    try:
        trades = data.fetch_recent_trades(limit=5)
        if trades:
            for t in trades:
                side_c = G if t["side"] == "BUY" else R
                icon = "▲" if t["side"] == "BUY" else "▼"
                print(
                    f"  {side_c}{icon} {t['side']:<4}{RST} "
                    f"${t['price']:>10,.2f}  "
                    f"×{t['qty']:.4f}  "
                    f"PnL={color_pnl(t['realized_pnl'])}  "
                    f"Fee={D}${t['commission']:.4f}{RST}"
                )
        else:
            print(f"  {D}No fills yet{RST}")
    except Exception:
        print(f"  {D}No fills yet{RST}")

    # ── Footer ───────────────────────────────────────────────────────
    print(f"\n{C}{'═' * W2}{RST}")
    print(f"  {D}Ctrl+C to exit  │  WS: {ws_icon}  │  "
          f"Last msg: {(time.time() * 1000 - rt['last_msg_ts']) / 1000:.1f}s ago{RST}" if rt["last_msg_ts"] > 0 else
          f"  {D}Ctrl+C to exit  │  WS: {ws_icon}{RST}")


def main():
    # Start WebSocket for real-time data
    print(f"{C}Starting real-time dashboard …{RST}")
    ws_stream.start()
    time.sleep(1)

    while True:
        try:
            display()
            time.sleep(2)
        except KeyboardInterrupt:
            print(f"\n  {Y}Dashboard stopped.{RST}")
            ws_stream.stop()
            break
        except Exception as e:
            print(f"\n  {R}[ERR] {e}{RST}")
            time.sleep(3)


if __name__ == "__main__":
    main()
