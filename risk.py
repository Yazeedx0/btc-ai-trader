"""
Risk control layer — validates every Claude decision against hard limits
before it reaches the execution engine.
"""

from __future__ import annotations

import config


class RiskManager:
    """Stateful risk gate that tracks consecutive losses and drawdown."""

    def __init__(self, starting_balance: float):
        self.starting_balance = starting_balance
        self.consecutive_losses = 0

    # ── Public API ───────────────────────────────────────────────────

    def validate(
        self,
        decision: dict,
        current_balance: float,
        open_positions: list[dict],
    ) -> tuple[bool, str]:
        """
        Return (allowed, reason).
        *allowed* is True when the trade may proceed.
        """
        action = decision.get("action", "HOLD")

        # HOLD is always allowed
        if action == "HOLD":
            return True, "HOLD — no action needed."

        # CLOSE is always allowed (closing risk is reducing risk)
        if action == "CLOSE":
            return True, "CLOSE — reducing exposure."

        # ── Drawdown check ───────────────────────────────────────────
        drawdown_pct = (
            (self.starting_balance - current_balance) / self.starting_balance * 100
            if self.starting_balance > 0
            else 0
        )
        if drawdown_pct >= config.MAX_DRAWDOWN_PCT:
            return False, (
                f"REJECTED: equity drawdown {drawdown_pct:.1f}% "
                f">= {config.MAX_DRAWDOWN_PCT}% limit."
            )

        # ── Consecutive losses ───────────────────────────────────────
        if self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            return False, (
                f"REJECTED: {self.consecutive_losses} consecutive losses "
                f">= {config.MAX_CONSECUTIVE_LOSSES} limit."
            )

        # ── Only one open position ───────────────────────────────────
        if open_positions:
            return False, "REJECTED: already have an open position."

        # ── Position size ────────────────────────────────────────────
        size_pct = decision.get("position_size_percent", 0)
        if size_pct > config.MAX_POSITION_SIZE_PCT:
            return False, (
                f"REJECTED: position_size_percent {size_pct} "
                f"> {config.MAX_POSITION_SIZE_PCT}% limit."
            )
        if size_pct <= 0:
            return False, "REJECTED: position_size_percent must be > 0."

        # ── Leverage ─────────────────────────────────────────────────
        leverage = decision.get("leverage", 0)
        if leverage > config.MAX_LEVERAGE:
            return False, (
                f"REJECTED: leverage {leverage} > {config.MAX_LEVERAGE}x limit."
            )
        if leverage < 1:
            return False, "REJECTED: leverage must be >= 1."

        # ── Confidence ───────────────────────────────────────────────
        confidence = decision.get("confidence", 0)
        if confidence < config.MIN_CONFIDENCE:
            return False, (
                f"REJECTED: confidence {confidence} "
                f"< {config.MIN_CONFIDENCE} threshold."
            )

        # ── Stop-loss / take-profit sanity ───────────────────────────
        sl = decision.get("stop_loss", 0)
        tp = decision.get("take_profit", 0)
        if sl <= 0 or tp <= 0:
            return False, "REJECTED: stop_loss and take_profit must be > 0."

        return True, "APPROVED."

    # ── Loss tracking ────────────────────────────────────────────────

    def record_trade_result(self, pnl: float) -> None:
        """Call after a position is closed to update the loss streak."""
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def reset_consecutive_losses(self) -> None:
        self.consecutive_losses = 0
