"""Amend-first diff between a resting order and the quote we want.

Kalshi, Amend Order (V2), fetched 2026-10-01
(https://docs.kalshi.com/api-reference/orders/amend-order-v2):

    "Amending only expiry or decreasing size preserves queue position.
     Increasing size or changing price forfeits queue position and places
     the order at the back of the queue."

Decrease Order (V2) is the size-down path. A cancel followed by a new order
always loses the queue and, if the cancel fails, can leave two orders on
the same side. So:

    same price, smaller size  -> decrease   (queue kept)
    same price, larger size   -> amend      (queue lost; one request)
    price change              -> amend      (queue lost; one request)
    inside a post-fill fade   -> do not top size back up
    no target                 -> cancel
    no resting order          -> create
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# A one-cent flicker is not worth forfeiting queue position. Size-up
# below this fraction is the same order, not a new one.
AMEND_PRICE_TICKS = 2
AMEND_SIZE_UP_FRAC = 0.10


@dataclass(frozen=True)
class Diff:
    action: str                 # keep | decrease | amend | cancel | create
    price_cents: Optional[int]
    size: float
    queue_preserved: bool
    reason: str


def plan_resting(
    resting_price: Optional[int],
    resting_size: Optional[float],
    target_price: Optional[int],
    target_size: float,
    *,
    eps: float = 1e-6,
    fade: bool = False,
    price_tick_threshold: int = AMEND_PRICE_TICKS,
    size_up_frac: float = AMEND_SIZE_UP_FRAC,
) -> Diff:
    """One resting order (or none) against one target.

    Price moves smaller than ``price_tick_threshold`` cents, and size-ups
    smaller than ``size_up_frac``, stay resting. Those writes were the
    churn that tripped the quote-rate caps.
    """
    if target_price is None or target_size <= eps:
        if resting_price is None:
            return Diff("keep", None, 0.0, True, "nothing_to_do")
        return Diff("cancel", None, 0.0, False, "target_pulled")
    if resting_price is None or resting_size is None:
        return Diff("create", int(target_price), float(target_size), False, "no_resting")
    same_px = int(resting_price) == int(target_price)
    if same_px and abs(float(resting_size) - float(target_size)) <= eps:
        return Diff("keep", int(target_price), float(target_size), True, "unchanged")
    if same_px and float(target_size) < float(resting_size) - eps:
        return Diff("decrease", int(target_price), float(target_size), True,
                    "size_down_keeps_queue")
    if same_px and fade and float(target_size) > float(resting_size) + eps:
        return Diff("keep", int(resting_price), float(resting_size), True,
                    "fade_no_topup")
    price_delta = 0 if same_px else abs(int(resting_price) - int(target_price))
    size_up = float(target_size) > float(resting_size) * (1.0 + float(size_up_frac)) + eps
    if price_delta < int(price_tick_threshold) and not size_up:
        if float(target_size) < float(resting_size) - eps:
            return Diff("decrease", int(resting_price), float(target_size), True,
                        "size_down_keeps_queue")
        return Diff("keep", int(resting_price), float(resting_size), True,
                    "within_amend_threshold")
    return Diff("amend", int(target_price), float(target_size), False,
                "price_or_size_up_loses_queue")
