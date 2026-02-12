"""
Trade logger — appends every decision and trade result to a CSV file.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone

import config


FIELDNAMES = [
    "timestamp",
    "action",
    "claude_decision",
    "entry_price",
    "close_price",
    "position_size",
    "leverage",
    "stop_loss",
    "take_profit",
    "pnl",
    "equity",
]


def _ensure_header() -> None:
    """Create the CSV with a header row if it doesn't exist yet."""
    if not os.path.exists(config.TRADE_LOG_FILE):
        with open(config.TRADE_LOG_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()


def log_trade(
    decision: dict,
    entry_price: float | None = None,
    close_price: float | None = None,
    position_size: float | None = None,
    leverage: int | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    pnl: float | None = None,
    equity: float | None = None,
) -> None:
    """Append a single row to the trade log CSV."""
    _ensure_header()
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": decision.get("action", ""),
        "claude_decision": json.dumps(decision),
        "entry_price": entry_price,
        "close_price": close_price,
        "position_size": position_size,
        "leverage": leverage,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "pnl": pnl,
        "equity": equity,
    }
    with open(config.TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writerow(row)
