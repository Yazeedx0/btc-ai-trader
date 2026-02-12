"""
Configuration module — loads all settings from environment variables.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Binance Futures Testnet ──────────────────────────────────────────
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_TESTNET_BASE = "https://testnet.binancefuture.com"

# ── Claude / Anthropic ──────────────────────────────────────────────
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# ── Trading parameters ──────────────────────────────────────────────
SYMBOL = "BTCUSDT"
TIMEFRAME = "5m"                     # base cycle — every 5 minutes
CANDLE_LIMIT = 200

# ── Risk limits ─────────────────────────────────────────────────────
MAX_POSITION_SIZE_PCT = 80        # % of balance
MAX_LEVERAGE = 20
MIN_CONFIDENCE = 0.5              # only trade when Claude is fairly sure
MAX_DRAWDOWN_PCT = 30             # protect capital
MAX_CONSECUTIVE_LOSSES = 10       # cool down after losing streak

# ── Logging ─────────────────────────────────────────────────────────
TRADE_LOG_FILE = "trade_log.csv"
