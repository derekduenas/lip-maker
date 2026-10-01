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

What was verified (2026-09-20)
------------------------------
Egress to docs.kalshi.com works from this machine, so the earlier
"UNVERIFIABLE" status is superseded. Against the official OpenAPI document
(https://docs.kalshi.com/openapi.yaml, "Kalshi Trade API Manual Endpoints"
v3.30.0, sha256 30e750f714ab37e1..., captured to
docs/venue_evidence/kalshi_create_order_20260920.json):

  * `post_only` DOES exist on CreateOrderRequest as a boolean. The field
    name this repo sends is correct.
  * `no_self_trade` does NOT exist in the current schema at all. The
    current field is `self_trade_prevention_type`, enum
    ['taker_at_cross', 'maker']. venue/kalshi.py was therefore sending a
    field that is both semantically wrong AND not in the API.
  * yes_price / no_price are integers bounded 1..99, which is what the
    0 < p < 100 guard below already enforced.
  * The legacy POST /portfolio/orders path this repo uses carries a
    deprecation notice ("no earlier than May 6, 2026" — already past),
    directing clients to /portfolio/events/orders.

What is STILL NOT verified
--------------------------
The *enforcement semantics* of `post_only` on an ORDER. The property has no
description in the spec. The only documented post_only behaviour belongs to
the QUOTE schema ("the quote creator's resting order will be cancelled
rather than crossed if it would take liquidity"). Suggestive, not a
statement about orders. Establishing it requires observing a live rejection,
which paper mode by definition cannot do.

Why the local check is a PREFLIGHT, not a proof
-----------------------------------------------
    A local non-crossing test cannot guarantee maker execution.

We evaluate the book as of our last observation, then the order travels to
Kalshi. Between those two instants another participant can lift the level we
priced against, and our "passive" bid arrives marketable. The check below is
therefore a necessary precondition we can enforce ourselves — it stops us
sending an order that is ALREADY crossing when we build it — but the only
thing that can stop a take on arrival is the exchange honouring post_only.

The arithmetic itself is sound and needs no documentation: on Kalshi both
sides are bids, mirror-priced, so a NO bid at n implies a YES offer at
100 - n, and a YES buy at p crosses iff p + best_no_bid >= 100. What it
cannot do is speak about the future.

Consequence: live execution stays BLOCKED while enforcement is unproven.
See require_live_execution_allowed().
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger(__name__)

# The field that requests maker-only behaviour. Isolated here so that
# renaming it, or migrating to a different order endpoint, is one edit.
MAKER_ONLY_FIELD = "post_only"

# Provenance for the two claims we must keep apart.
MAKER_ONLY_SPEC_SOURCE = "https://docs.kalshi.com/openapi.yaml"
MAKER_ONLY_SPEC_VERSION = "3.30.0"
MAKER_ONLY_SPEC_CAPTURED = "2026-09-20"
MAKER_ONLY_SPEC_EVIDENCE = "docs/venue_evidence/kalshi_create_order_20260920.json"

# The field EXISTS in the official CreateOrderRequest schema. Verified.
MAKER_ONLY_FIELD_VERIFIED = True

# Whether the exchange is known to REJECT/cancel an order that would cross.
# The spec documents no semantics for post_only on an order, so this stays
# False until a live rejection is observed. Do not promote a conservative
# assumption into a verified fact: the whole point of the flag is the case
# the local preflight cannot cover (the book moving in transit).
#
# Kalshi demo evidence (2026-09-30) is recorded, but this global flag stays
# False: it also gates venues that were not on that wire (Polymarket US,
# the quote manager). Kalshi has its own switch below.
MAKER_ONLY_ENFORCEMENT_VERIFIED = False

# Kalshi-only. Default False, so KalshiAdapter live writes stay blocked.
# Set only via enable_kalshi_maker_only_enforcement(). Does not flip the
# global flag and does not allow the production host.
KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED = False
KALSHI_POST_ONLY_EVIDENCE = "docs/venue_evidence/kalshi_post_only_demo_20260930.md"
KALSHI_POST_ONLY_ACK = "kalshi-demo-2026-09-30-post-only-cross"

# The obsolete field venue/kalshi.py sent. Kept as a named constant so the
# guard below can name it in the error rather than hard-coding a string.
OBSOLETE_STP_FIELD = "no_self_trade"
CURRENT_STP_FIELD = "self_trade_prevention_type"
CURRENT_STP_VALUES = ("taker_at_cross", "maker")

# Kalshi binaries clear at $1: yes_price + no_price = 100 cents.
CONTRACT_CENTS = 100

# Verified against CreateOrderRequest.time_in_force in the official spec
# (v3.30.0). "GTC" and "GTT" are NOT accepted values; the spec says so
# explicitly for GTT. venue/kalshi.py was sending "GTC".
TIME_IN_FORCE_VALUES = ("fill_or_kill", "good_till_canceled", "immediate_or_cancel")

# Verified price bounds: yes_price / no_price are integers 1..99.
PRICE_MIN_CENTS, PRICE_MAX_CENTS = 1, 99


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
    """PREFLIGHT: would this buy be crossing AS OF THE BOOK WE LAST SAW?

    This is not a guarantee of maker execution. It answers a question about
    the past (the book at our last observation), and the order executes in
    the future (on arrival at Kalshi). A level we priced against can be
    lifted in transit, making a locally-passive order marketable. Only
    exchange-side post_only can prevent that; see the module docstring.

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
        if time_in_force not in TIME_IN_FORCE_VALUES:
            raise MakerSafetyError(
                f"time_in_force {time_in_force!r} is not accepted by the venue; "
                f"valid values are {', '.join(TIME_IN_FORCE_VALUES)} "
                f"(verified against {MAKER_ONLY_SPEC_SOURCE} "
                f"v{MAKER_ONLY_SPEC_VERSION})")
        body["time_in_force"] = time_in_force
    return body


class LiveExecutionBlocked(RuntimeError):
    """Live order transmission attempted while maker enforcement is unproven."""


def enable_kalshi_maker_only_enforcement(acknowledgement: str) -> None:
    """Mark Kalshi ``post_only`` enforced for this process. Default is off.

    The demo wire on 2026-09-30 (``demo-api.kalshi.co``, market
    ``KXRAIN-26SEP30-DTW``) rejected crossing V2 orders with HTTP 400
    ``invalid_order`` / ``post only cross``, and an amend to a crossing
    price returned HTTP 200 with ``remaining_count`` 0 and no fill. That
    evidence is written up in ``KALSHI_POST_ONLY_EVIDENCE``.

    Passing ``KALSHI_POST_ONLY_ACK`` sets ``KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED``
    only. ``MAKER_ONLY_ENFORCEMENT_VERIFIED`` stays False, so the quote
    manager and Polymarket US stay blocked. Production hosts stay blocked
    until ``KalshiRestTransport(..., allow_production=True)``.
    """
    global KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED
    if acknowledgement != KALSHI_POST_ONLY_ACK:
        raise LiveExecutionBlocked(
            "refusing to mark Kalshi post_only verified without the demo "
            f"acknowledgement; evidence is {KALSHI_POST_ONLY_EVIDENCE}")
    KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED = True


def require_live_execution_allowed(*, venue: str = "") -> None:
    """Raise unless we may legitimately send a LIVE order.

    The directive is explicit: a local non-crossing check cannot guarantee
    maker execution, so exchange-enforced post_only must be verified before
    live execution. The field is verified to exist. Enforcement is per venue.

    ``venue="kalshi"`` consults ``KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED``,
    which defaults False. Every other caller, including the quote manager,
    consults ``MAKER_ONLY_ENFORCEMENT_VERIFIED``, which also defaults False.
    Enabling the Kalshi switch does not unblock those callers.

    Paper mode never reaches this: it sends nothing.
    """
    if not MAKER_ONLY_FIELD_VERIFIED:
        raise LiveExecutionBlocked(
            f"{MAKER_ONLY_FIELD} is not verified against the venue schema")
    if venue == "kalshi":
        if not KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED:
            raise LiveExecutionBlocked(
                "LIVE EXECUTION BLOCKED for Kalshi: post_only was observed "
                "on the demo exchange (2026-09-30, see "
                f"{KALSHI_POST_ONLY_EVIDENCE}) but "
                "KALSHI_MAKER_ONLY_ENFORCEMENT_VERIFIED is still False. "
                "Call enable_kalshi_maker_only_enforcement with "
                f"{KALSHI_POST_ONLY_ACK!r} to acknowledge that evidence. "
                "That call does not allow the production host.")
        return
    if not MAKER_ONLY_ENFORCEMENT_VERIFIED:
        raise LiveExecutionBlocked(
            f"LIVE EXECUTION BLOCKED: `{MAKER_ONLY_FIELD}` exists in "
            f"{MAKER_ONLY_SPEC_SOURCE} v{MAKER_ONLY_SPEC_VERSION} but its "
            "enforcement semantics for orders are undocumented. The local "
            "non-crossing test is a PREFLIGHT against the last observed "
            "book, not proof of maker execution — the book can move in "
            "transit. Observe a real post_only rejection, record it in "
            f"{MAKER_ONLY_SPEC_EVIDENCE}, then set "
            "MAKER_ONLY_ENFORCEMENT_VERIFIED = True.")


# Create Order (V2). Legacy POST /portfolio/orders is deprecated no earlier
# than 6 May 2026 (OpenAPI 3.32.0, fetched 2026-10-01).
V2_CREATE_PATH = "/portfolio/events/orders"


def to_event_order_v2(body: dict, *, order_group_id: Optional[str] = None) -> dict:
    """Translate a maker-safe legacy body into a Create Order (V2) body.

    V2 quotes the YES book only (docs: BookSide). A YES buy at p cents is
    ``side=bid``, ``price=p/100``. A NO buy at q cents is economically a YES
    sell at ``1 - q/100``, so ``side=ask``. ``count`` and ``price`` are
    fixed-point strings. ``time_in_force`` and ``self_trade_prevention_type``
    are required. ``post_only`` stays set. ``taker_at_cross`` cancels our
    incoming order if it would trade against our own resting order, which is
    the STP mode that does not pull the quote we are trying to keep.
    """
    assert_maker_safe(body)
    side = body["side"]
    if side == "yes":
        book_side = "bid"
        dollars = int(body["yes_price"]) / CONTRACT_CENTS
    elif side == "no":
        book_side = "ask"
        dollars = (CONTRACT_CENTS - int(body["no_price"])) / CONTRACT_CENTS
    else:
        raise MakerSafetyError(f"invalid side {side!r}")
    tif = body.get("time_in_force") or "good_till_canceled"
    if tif not in TIME_IN_FORCE_VALUES:
        raise MakerSafetyError(f"time_in_force {tif!r} is not a V2 value")
    out = {
        "ticker": body["ticker"],
        "client_order_id": body["client_order_id"],
        "side": book_side,
        "count": f"{float(body['count']):.2f}",
        "price": f"{dollars:.4f}",
        "time_in_force": tif,
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": True,
    }
    if order_group_id:
        out["order_group_id"] = order_group_id
    return out


def assert_maker_safe(body: dict) -> None:
    """Last line of defence before transmission.

    Rejects a body that lost its maker flag, or that carries the obsolete
    `no_self_trade` *instead of* the maker flag — the specific substitution
    that made venue/kalshi.py unsafe. `no_self_trade` is not merely weaker;
    it is not in the current schema at all (the field is
    `self_trade_prevention_type`), so an adapter sending it has neither
    self-trade prevention nor maker protection.
    """
    if body.get("action") != "buy":
        raise MakerSafetyError(f"only passive buys are supported, got {body.get('action')!r}")
    if body.get("type") != "limit":
        raise MakerSafetyError(f"maker orders must be limit, got {body.get('type')!r}")
    if not body.get(MAKER_ONLY_FIELD):
        extra = ""
        if body.get(OBSOLETE_STP_FIELD):
            extra = (f" — `{OBSOLETE_STP_FIELD}` only blocks trading against "
                     "your OWN order; it does not prevent crossing a "
                     "stranger's offer, and it is not in the current schema "
                     f"(that field is `{CURRENT_STP_FIELD}`: "
                     f"{'/'.join(CURRENT_STP_VALUES)})")
        raise MakerSafetyError(
            f"order is missing {MAKER_ONLY_FIELD}{extra}")
    if body.get(OBSOLETE_STP_FIELD):
        raise MakerSafetyError(
            f"`{OBSOLETE_STP_FIELD}` is not a field in the current Kalshi "
            f"schema (v{MAKER_ONLY_SPEC_VERSION}); use `{CURRENT_STP_FIELD}`")
