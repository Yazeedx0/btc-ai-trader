# Claude × Binance Futures Testnet — Paper Trader

A live paper-trading system where **Claude** autonomously makes trading decisions on **BTCUSDT perpetual futures** using the Binance Futures Testnet.

## Architecture

```
main.py            Event loop — waits for each 5m candle close, orchestrates the cycle
data.py            Fetches OHLCV candles & account info, computes EMA20/50, RSI14, ATR14
claude_client.py   Builds the context payload, calls the Claude API, parses JSON decisions
risk.py            Validates every decision against hard risk limits before execution
execution.py       Places market orders, stop-loss, and take-profit on Binance Testnet
logger.py          Appends every decision & trade result to a CSV log
config.py          Loads all settings from environment variables
```

## Risk Controls

| Rule | Limit |
|------|-------|
| Max position size | 10 % of balance |
| Max leverage | 5× |
| Min confidence | 0.4 |
| Concurrent positions | 1 |
| Equity drawdown halt | 15 % from start |
| Consecutive loss halt | 5 losses |

## Quick Start

```bash
# 1. Clone & enter the project
cd binance-test

# 2. Create a virtual environment
python3 -m venv .venv && source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
cp .env.example .env
# Edit .env and fill in your keys

# 5. Run
python main.py
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BINANCE_API_KEY` | ✅ | Binance Futures **Testnet** API key |
| `BINANCE_API_SECRET` | ✅ | Binance Futures **Testnet** API secret |
| `CLAUDE_API_KEY` | ✅ | Anthropic API key |
| `CLAUDE_MODEL` | ❌ | Model override (default `claude-sonnet-4-20250514`) |

## Trade Log

All decisions are logged to `trade_log.csv` with columns:

`timestamp, action, claude_decision, entry_price, close_price, position_size, leverage, stop_loss, take_profit, pnl, equity`

## How It Works

1. **Wait** for the next 5-minute candle to close.
2. **Fetch** the last 200 OHLCV candles and compute indicators.
3. **Fetch** account balance, open positions, and recent trades.
4. **Send** all context to Claude and receive a JSON decision.
5. **Validate** the decision against risk rules.
6. **Execute** the order (or skip if rejected / HOLD).
7. **Log** everything and repeat.
