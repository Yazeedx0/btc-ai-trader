#!/usr/bin/env python3
"""
Live dashboard — P&L focused, refreshes every 30 seconds.
"""

import os
import time
import csv
from datetime import datetime, timezone

import config
import data


# ── ANSI colors ──────────────────────────────────────────────────────
G = "\033[92m"   # green
R = "\033[91m"   # red
Y = "\033[93m"   # yellow
C = "\033[96m"   # cyan
W = "\033[97m"   # white/bold
D = "\033[90m"   # dim/gray
B = "\033[1m"    # bold
RST = "\033[0m"  # reset

REFRESH = 30  # seconds


def clear():
    os.system("clear")


def ts_now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def color_pnl(val: float) -> str:
    if val > 0:
        return f"{G}+${val:,.2f}{RST}"
    elif val < 0:
        return f"{R}-${abs(val):,.2f}{RST}"
    return f"{D}$0.00{RST}"


def color_pct(val: float) -> str:
    if val > 0:
        return f"{G}+{val:.2f}%{RST}"
    elif val < 0:
        return f"{R}{val:.2f}%{RST}"
    return f"{D}0.00%{RST}"


def read_trade_log() -> list[dict]:
    if not os.path.exists(config.TRADE_LOG_FILE):
        return []
    with open(config.TRADE_LOG_FILE) as f:
        return list(csv.DictReader(f))


def compute_session_stats(rows: list[dict]) -> dict:
    """Compute total P&L, wins, losses from trade log."""
    trades_with_pnl = [r for r in rows if r.get("pnl") and r["pnl"] != ""]
    if not trades_with_pnl:
        return {"total_pnl": 0, "wins": 0, "losses": 0, "total_trades": 0,
                "win_rate": 0, "best": 0, "worst": 0, "total_fees": 0}

    pnls = [float(r["pnl"]) for r in trades_with_pnl]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    return {
        "total_pnl": sum(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "total_trades": len(pnls),
        "win_rate": len(wins) / len(pnls) * 100 if pnls else 0,
        "best": max(pnls) if pnls else 0,
        "worst": min(pnls) if pnls else 0,
        "avg_win": sum(wins) / len(wins) if wins else 0,
        "avg_loss": sum(losses) / len(losses) if losses else 0,
    }


def display():
    clear()
    W2 = 72

    print(f"{B}{C}{'═' * W2}{RST}")
    print(f"{B}{C}  💰 P&L DASHBOARD — {config.SYMBOL}{RST}")
    print(f"{C}  {ts_now()}  │  Refresh: {REFRESH}s{RST}")
    print(f"{B}{C}{'═' * W2}{RST}")

    # ── Account & P&L ────────────────────────────────────────────────
    try:
        account = data.fetch_account_info()
        balance = account["usdt_balance"]
        unrealized = account["unrealized_pnl"]
        equity = balance + unrealized
        positions = account["positions"]
        price = data.fetch_current_price()

        print(f"\n  {B}💲 BTC Price:{RST}   {W}${price:>12,.2f}{RST}")
        print(f"  {B}📊 Balance:{RST}     {W}${balance:>12,.2f}{RST}")
        print(f"  {B}📈 Unrealised:{RST}  {color_pnl(unrealized):>24s}")
        print(f"  {B}💎 Equity:{RST}      {W}${equity:>12,.2f}{RST}")

        # ── Open Positions ───────────────────────────────────────────
        print(f"\n  {D}{'─' * (W2 - 4)}{RST}")
        if positions:
            print(f"  {B}📌 OPEN POSITIONS{RST}\n")
            for p in positions:
                side_c = G if p["side"] == "LONG" else R
                icon = "▲" if p["side"] == "LONG" else "▼"
                pnl = p["unrealized_pnl"]
                entry = p["entry_price"]
                size = p["size"]
                lev = p["leverage"]
                notional = entry * size if entry > 0 else 0
                pnl_pct = (pnl / notional * 100) if notional > 0 else 0

                print(f"    {side_c}{icon} {p['side']}{RST}  {lev}x")
                print(f"      Entry:  ${entry:>12,.2f}")
                print(f"      Size:   {size:.4f} BTC  (${notional:,.2f})")
                print(f"      P&L:    {color_pnl(pnl)}  ({color_pct(pnl_pct)})")
                print()
        else:
            print(f"  {D}  No open positions{RST}\n")

    except Exception as e:
        print(f"  {R}Account error: {e}{RST}")

    # ── Session Stats ────────────────────────────────────────────────
    rows = read_trade_log()
    stats = compute_session_stats(rows)

    print(f"  {D}{'─' * (W2 - 4)}{RST}")
    print(f"  {B}📊 SESSION STATS{RST}\n")

    total_pnl = stats["total_pnl"]
    print(f"    Total P&L:      {color_pnl(total_pnl)}")
    print(f"    Trades:         {W}{stats['total_trades']}{RST}  "
          f"({G}{stats['wins']}W{RST} / {R}{stats['losses']}L{RST})")
    print(f"    Win Rate:       {color_pct(stats['win_rate'])}")
    print(f"    Best Trade:     {color_pnl(stats['best'])}")
    print(f"    Worst Trade:    {color_pnl(stats['worst'])}")
    if stats["total_trades"] > 0:
        print(f"    Avg Win:        {color_pnl(stats['avg_win'])}")
        print(f"    Avg Loss:       {color_pnl(stats['avg_loss'])}")

    # ── Recent Trades (last 10) ──────────────────────────────────────
    print(f"\n  {D}{'─' * (W2 - 4)}{RST}")
    print(f"  {B}📜 RECENT TRADES{RST}\n")

    trades_with_pnl = [r for r in rows if r.get("action") in ("BUY", "SELL", "CLOSE") and r.get("pnl")]
    recent = trades_with_pnl[-10:] if trades_with_pnl else []

    if recent:
        print(f"    {D}{'Time':<10} {'Action':<6} {'Entry':>10} {'P&L':>12} {'Equity':>12}{RST}")
        print(f"    {D}{'─' * 54}{RST}")
        for r in recent:
            ts = r.get("timestamp", "")
            t_part = ts[11:19] if len(ts) > 19 else ts[:8]
            act = r.get("action", "?")
            act_c = G if act == "BUY" else R if act == "SELL" else Y
            entry = r.get("entry_price", "")
            pnl = r.get("pnl", "")
            eq = r.get("equity", "")

            entry_s = f"${float(entry):>9,.2f}" if entry else f"{'—':>10}"
            pnl_s = color_pnl(float(pnl)) if pnl else f"{'—':>12}"
            eq_s = f"${float(eq):>10,.2f}" if eq else f"{'—':>12}"

            print(f"    {D}{t_part}{RST} {act_c}{act:<6}{RST} {entry_s} {pnl_s:>24s} {eq_s}")
    else:
        print(f"    {D}No closed trades yet{RST}")

    # ── Cumulative P&L Chart (text) ──────────────────────────────────
    all_pnls = [float(r["pnl"]) for r in rows if r.get("pnl") and r["pnl"] != ""]
    if len(all_pnls) >= 2:
        print(f"\n  {D}{'─' * (W2 - 4)}{RST}")
        print(f"  {B}📉 CUMULATIVE P&L{RST}\n")
        cumulative = []
        running = 0
        for p in all_pnls:
            running += p
            cumulative.append(running)

        max_val = max(abs(v) for v in cumulative) if cumulative else 1
        chart_width = 40

        for i, val in enumerate(cumulative[-15:]):  # last 15 trades
            bar_len = int(abs(val) / max_val * chart_width) if max_val > 0 else 0
            if val >= 0:
                bar_s = f"{G}{'█' * bar_len}{RST}"
                print(f"    {D}#{len(cumulative) - 14 + i if len(cumulative) > 15 else i + 1:>3}{RST} {bar_s} {color_pnl(val)}")
            else:
                bar_s = f"{R}{'█' * bar_len}{RST}"
                print(f"    {D}#{len(cumulative) - 14 + i if len(cumulative) > 15 else i + 1:>3}{RST} {bar_s} {color_pnl(val)}")

    # ── Footer ───────────────────────────────────────────────────────
    print(f"\n{C}{'═' * W2}{RST}")
    print(f"  {D}Ctrl+C to exit  │  Updates every {REFRESH}s{RST}")


def main():
    while True:
        try:
            display()
            time.sleep(REFRESH)
        except KeyboardInterrupt:
            print(f"\n  {Y}Dashboard stopped.{RST}")
            break
        except Exception as e:
            print(f"\n  {R}[ERR] {e}{RST}")
            time.sleep(5)


if __name__ == "__main__":
    main()
