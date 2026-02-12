#!/usr/bin/env python3
"""
close_position.py — Manually close open positions.

Usage:
  python close_position.py          # shows positions, asks which to close
  python close_position.py all      # closes ALL positions immediately
  python close_position.py BTCUSDT  # closes BTCUSDT immediately
"""

import sys
import data
import execution


def show_positions(positions: list[dict], balance: float) -> None:
    print(f"\n  Balance: ${balance:,.2f} USDT\n")
    if not positions:
        print("  ❌ No open positions.")
        return
    print(f"  {'#':<4} {'Symbol':<12} {'Side':<8} {'Size':<12} {'Entry':>12} {'uPnL':>12} {'Lev':>6}")
    print("  " + "─" * 66)
    for i, p in enumerate(positions, 1):
        pnl_str = f"${p['unrealized_pnl']:+,.2f}"
        print(
            f"  {i:<4} {p['symbol']:<12} {p['side']:<8} {p['size']:<12} "
            f"${p['entry_price']:>11,.2f} {pnl_str:>12} {p['leverage']:>5}x"
        )
    print()


def close_symbol(symbol: str) -> None:
    print(f"  ⏳ Closing {symbol} ...")
    result = execution.execute_close(symbol=symbol)
    if result:
        print(f"  ✅ Closed {symbol} @ ${result['close_price']:,.2f} (qty={result['quantity']})")
    else:
        print(f"  ❌ No position to close for {symbol}.")


def main():
    account = data.fetch_account_info()
    positions = account["positions"]

    # Quick mode: python close_position.py all
    if len(sys.argv) > 1:
        arg = sys.argv[1].strip().upper()
        if arg == "ALL":
            if not positions:
                print("  No open positions.")
                return
            for p in positions:
                close_symbol(p["symbol"])
        else:
            close_symbol(arg)
        # Show final balance
        new_acc = data.fetch_account_info()
        print(f"\n  💰 New balance: ${new_acc['usdt_balance']:,.2f} USDT")
        return

    # Interactive mode
    show_positions(positions, account["usdt_balance"])
    if not positions:
        return

    choice = input("  Enter # to close (or 'all' / 'q' to quit): ").strip()

    if choice.lower() == "q":
        print("  Cancelled.")
        return

    if choice.lower() == "all":
        for p in positions:
            close_symbol(p["symbol"])
    elif choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(positions):
            close_symbol(positions[idx]["symbol"])
        else:
            print("  ❌ Invalid number.")
            return
    else:
        print("  ❌ Invalid input.")
        return

    new_acc = data.fetch_account_info()
    print(f"\n  💰 New balance: ${new_acc['usdt_balance']:,.2f} USDT")


if __name__ == "__main__":
    main()
