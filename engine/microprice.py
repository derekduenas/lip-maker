"""Microprice — imbalance-weighted mid as a better fair-value anchor than (bid+ask)/2.

Stoikov 2018 ("The Micro-Price: A High-Frequency Estimator of Future Prices",
SSRN 2970694): the order-book imbalance predicts which side trades through next;
weighting the mid by the imbalance gives a martingale-corrected fair value that
beats arithmetic mid as a one-step-ahead estimator.

For a Kalshi binary book:
    yes_bid = best YES buy price        (people willing to pay this for YES)
    yes_ask = best YES sell price       (people willing to sell YES at this)
            = 100 - best NO bid         (mirror equivalence when no explicit ask)

    microprice = (yes_ask * yes_bid_size + yes_bid * yes_ask_size)
                 / (yes_bid_size + yes_ask_size)

The intuition: a large bid relative to ask means buyers are more eager →
next trade is more likely to lift the offer → mid leans toward the ask.
Symmetrically, large ask leans toward the bid.

Clamped to [1, 99] to avoid edge cases at boundaries (Kalshi rejects orders
at 0 / 100 anyway).

USAGE
-----
    from execution.kalshi_ws import BookState
    from engine.microprice import microprice_yes

    mp = microprice_yes(book)         # float in cents [1.0, 99.0] or None
"""
from __future__ import annotations

from typing import Optional


def microprice_cents(bid: float, ask: float,
                     bid_size: float, ask_size: float) -> Optional[float]:
    """Imbalance-weighted mid in cents. Returns None if inputs are degenerate.

    Args:
        bid: best bid price (cents)
        ask: best ask price (cents)
        bid_size: size at best bid (contracts)
        ask_size: size at best ask (contracts)

    Returns:
        Microprice in cents, clamped to [1.0, 99.0], or None if:
          - sizes sum to zero (no liquidity to weight by)
          - bid >= ask (crossed/locked book — caller should treat as stale)
    """
    if bid_size + ask_size <= 0:
        return None
    if bid >= ask:
        # Crossed/locked book — microprice is meaningless. Caller should
        # fall back to arithmetic mid or skip this update.
        return None
    mp = (ask * bid_size + bid * ask_size) / (bid_size + ask_size)
    # Clamp — Kalshi binary range
    return max(1.0, min(99.0, mp))


def microprice_yes(book) -> Optional[float]:
    """Microprice of the YES side of a Kalshi book.

    Derives YES ask from explicit yes_asks if present, else from no_bid
    mirror (yes_ask = 100 - no_bid). This handles Kalshi's common case
    where the book lists only bid sides for YES and NO.

    Args:
        book: execution.kalshi_ws.BookState

    Returns:
        Microprice in cents [1.0, 99.0] or None if YES side is incomplete.
    """
    yes_bid = book.best_yes_bid()
    if yes_bid is None:
        return None

    yes_ask = book.best_yes_ask()
    if yes_ask is not None:
        ask_price = float(yes_ask.price_cents)
        ask_size = float(yes_ask.size)
    else:
        no_bid = book.best_no_bid()
        if no_bid is None:
            return None
        ask_price = 100.0 - float(no_bid.price_cents)
        ask_size = float(no_bid.size)

    return microprice_cents(
        bid=float(yes_bid.price_cents),
        ask=ask_price,
        bid_size=float(yes_bid.size),
        ask_size=ask_size,
    )


def microprice_no(book) -> Optional[float]:
    """Microprice of the NO side (mirror of YES). 100 - microprice_yes."""
    mp_yes = microprice_yes(book)
    if mp_yes is None:
        return None
    return max(1.0, min(99.0, 100.0 - mp_yes))


def arithmetic_mid_yes(book) -> Optional[float]:
    """Plain (bid+ask)/2 mid for the YES side. Used as fallback / comparison."""
    yes_bid = book.best_yes_bid()
    if yes_bid is None:
        return None
    yes_ask = book.best_yes_ask()
    if yes_ask is not None:
        ask_price = float(yes_ask.price_cents)
    else:
        no_bid = book.best_no_bid()
        if no_bid is None:
            return None
        ask_price = 100.0 - float(no_bid.price_cents)
    return (float(yes_bid.price_cents) + ask_price) / 2.0


def microprice_tilt_cents(book) -> Optional[float]:
    """How far the microprice leans away from the arithmetic mid.

    Positive = microprice leans toward the ask (size pressure on the bid).
    Negative = microprice leans toward the bid (size pressure on the ask).

    Useful as an order-flow-imbalance proxy at zero extra cost.
    """
    mp = microprice_yes(book)
    mid = arithmetic_mid_yes(book)
    if mp is None or mid is None:
        return None
    return mp - mid
