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

# ── Google Gemini ────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Pro for deep 5m analysis (high reasoning), Flash for fast 1m checks
GEMINI_PRO_MODEL = os.getenv("GEMINI_PRO_MODEL", "gemini-2.5-pro")
GEMINI_FLASH_MODEL = os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash")

# ── Trading parameters ──────────────────────────────────────────────
SYMBOL = "BTCUSDT"
TIMEFRAME = "5m"                     # base cycle — every 5 minutes
CANDLE_LIMIT = 200

# ── Risk limits ─────────────────────────────────────────────────────
MAX_POSITION_SIZE_PCT = 90        # % of balance
MAX_LEVERAGE = 20                 # smart leverage
MIN_CONFIDENCE = 0.3              # reasonable threshold
MAX_DRAWDOWN_PCT = 50             # protect capital
MAX_CONSECUTIVE_LOSSES = 12       # cool down after losing streak

# ── Position management ─────────────────────────────────────────────
QUICK_CHECK_SECONDS = 60          # check open position every 60s
MAX_PYRAMID_ADDS = 2              # max times to add to a winning position

# ── Logging ─────────────────────────────────────────────────────────
TRADE_LOG_FILE = "trade_log.csv"
