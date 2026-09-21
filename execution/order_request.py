"""The single place this repository decides what an order request looks like
and what makes it maker-safe (2026-09-21).

Why this module exists
----------------------
Two adapters disagreed:

  execution/quote_manager.py  sent  {"post_only": True}
  venue/kalshi.py             sent  {"no_self_trade": True}

with a comment asserting `post_only` does not exist. Those are not equivalent.
`no_self_trade` only prevents trading against *your own* resting order; it
does nothing to stop your bid crossing a stranger's ask and paying taker fees.
A "maker" strategy running through that adapter could silently take liquidity
— and a LIP maker that takes liquidity loses the rebate and pays the fee.

Which flag Kalshi actually honours is UNVERIFIED. docs.kalshi.com is blocked
by this environment's egress proxy (403 to CONNECT), so no claim about the
current API could be checked. See docs/CLAUDE_INDEPENDENT_ASSESSMENT.md §0.

Design consequence of that uncertainty
--------------------------------------
Do not make maker safety depend on a flag we cannot verify. This module makes
the LOCAL, CHECKABLE invariant primary:

    A buy order is maker-safe iff its limit price is strictly below the best
    opposing offer, so it cannot execute on arrival.

On Kalshi both sides are bids (buy YES / buy NO) and the two sides are
mirror-priced: a NO bid at n implies a YES offer at 100 - n. So a YES buy at
`p` crosses iff `p >= 100 - best_no_bid`, i.e. iff `p + best_no_bid >= 100`.
That check needs no documentation to be correct — it follows from the
contract paying $1.

The exchange flag is then belt-and-braces, declared in one constant so that
verifying or changing it later is a single edit rather than a hunt.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger(__name__)

# The field we believe requests maker-only (reject-if-would-cross) behaviour.
# UNVERIFIED against current Kalshi docs — see module docstring. Isolated here
# so that confirming it, renaming it, or migrating to a different order
# endpoint is one edit in one file.
MAKER_ONLY_FIELD = "post_only"
MAKER_ONLY_VERIFIED = False

# Kalshi binaries clear at $1: yes_price + no_price = 100 cents.
CONTRACT_CENTS = 100


class MakerSafetyError(ValueError):
    """An order would have crossed the spread, or lacked maker protection."""


@dataclass(frozen=True)
class CrossCheck:
    """Result of the local non-crossing test."""
    safe: bool
    reason: str = ""
    implied_opposing_price: Optional[int] = None

    def __bool__(self) -> bool:
        return self.safe


def would_cross(side: str, price_cents: int, *,
                best_opposing_bid_cents: Optional[int]) -> CrossCheck:
    """Would a buy of `side` at `price_cents` execute immediately?

    `best_opposing_bid_cents` is the best bid on the OTHER side of the same
    market (for a YES buy, pass the best NO bid). A NO bid at n implies a YES
    offer at CONTRACT_CENTS - n, so our YES buy crosses iff it reaches that
    offer.

    Unknown opposing depth returns unsafe: absence of evidence that we are
    safe is not evidence of safety, and quoting is always deferrable.
    """
    if side not in ("yes", "no"):
        return CrossCheck(False, f"invalid side {side!r}")
    if best_opposing_bid_cents is None:
        return CrossCheck(False, "opposing side unknown; cannot prove non-crossing")
    implied = CONTRACT_CENTS - int(best_opposing_bid_cents)
    if int(price_cents) >= implied:
        return CrossCheck(
            False,
            f"{side} buy @{price_cents}c would lift the implied offer @{implied}c "
            f"(opposing bid {best_opposing_bid_cents}c) — that is a TAKE, not a make",
            implied)
    return CrossCheck(True, "", implied)


def build_limit_order(*, ticker: str, side: str, price_cents: int,
                      size_contracts: int, client_order_id: str,
                      best_opposing_bid_cents: Optional[int] = None,
                      enforce_non_crossing: bool = True,
                      time_in_force: Optional[str] = None) -> dict:
    """Build one maker-only buy-limit request body.

    Every order this repository sends should come from here, so that the
    maker contract is stated once.

    Raises MakerSafetyError when the order is not provably passive and
    `enforce_non_crossing` is set. Callers that genuinely want to cross must
    say so explicitly by passing enforce_non_crossing=False — which makes
    taking a deliberate, greppable act rather than an accident of which
    adapter happened to be imported.
    """
    if side not in ("yes", "no"):
        raise MakerSafetyError(f"invalid side {side!r}")
    price_cents = int(price_cents)
    size_contracts = int(size_contracts)
    if not (0 < price_cents < CONTRACT_CENTS):
        # 0 and 100 are rejected by the venue as invalid prices.
        raise MakerSafetyError(f"price {price_cents}c outside (0, {CONTRACT_CENTS})")
    if size_contracts <= 0:
        raise MakerSafetyError(f"non-positive size {size_contracts}")
    if not client_order_id:
        raise MakerSafetyError("client_order_id is required for idempotency")

    if enforce_non_crossing:
        check = would_cross(side, price_cents,
                            best_opposing_bid_cents=best_opposing_bid_cents)
        if not check.safe:
            raise MakerSafetyError(check.reason)

    body = {
        "ticker": ticker,
        "side": side,
        "action": "buy",
        "type": "limit",
        "count": size_contracts,
        "client_order_id": client_order_id,
        # Belt-and-braces on top of the local non-crossing proof above.
        MAKER_ONLY_FIELD: True,
    }
    body["yes_price" if side == "yes" else "no_price"] = price_cents
    if time_in_force:
        body["time_in_force"] = time_in_force
    return body


def assert_maker_safe(body: dict) -> None:
    """Last line of defence before transmission.

    Rejects a body that lost its maker flag, or that carries `no_self_trade`
    *instead of* the maker flag — the specific substitution that made
    venue/kalshi.py unsafe.
    """
    if body.get("action") != "buy":
        raise MakerSafetyError(f"only passive buys are supported, got {body.get('action')!r}")
    if body.get("type") != "limit":
        raise MakerSafetyError(f"maker orders must be limit, got {body.get('type')!r}")
    if not body.get(MAKER_ONLY_FIELD):
        extra = ""
        if body.get("no_self_trade"):
            extra = (" — `no_self_trade` only blocks trading against your OWN "
                     "order; it does not prevent crossing a stranger's offer")
        raise MakerSafetyError(
            f"order is missing {MAKER_ONLY_FIELD}{extra}")
