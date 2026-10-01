"""Single source of truth for the account's capital.

Three numbers used to coexist: ``BANKROLL_USD`` defaulted to $80, the paper
ledger opened at $5,000, and the deployed unit file set ``LIP_BANKROLL=2000``.
Risk caps that read one of those and sizing that read another were not
looking at the same account.

Precedence, first hit wins:

1. ``LIP_BANKROLL`` — what the operator says is deployed.
2. ``LIP_ACCOUNT_USD`` — the paper/ledger opening cash.
3. ``DEFAULT_CAPITAL_USD`` ($5,000), the figure ``engine.account_ledger``
   already treated as the account.

This module does not import ``config.settings``. Settings reads from here so
the two cannot drift.
"""
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

DEFAULT_CAPITAL_USD = Decimal("5000")


class BankrollConfigError(ValueError):
    """The operator's capital env var is not a positive number."""


def capital_usd() -> Decimal:
    """Capital that risk, the ledger, and the pool selector must share."""
    raw = os.getenv("LIP_BANKROLL")
    source = "LIP_BANKROLL"
    if raw is None or str(raw).strip() == "":
        raw = os.getenv("LIP_ACCOUNT_USD")
        source = "LIP_ACCOUNT_USD"
    if raw is None or str(raw).strip() == "":
        return DEFAULT_CAPITAL_USD
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation as e:
        raise BankrollConfigError(f"{source}={raw!r} is not a number") from e
    if value <= 0:
        raise BankrollConfigError(f"{source}={value} must be positive")
    return value
