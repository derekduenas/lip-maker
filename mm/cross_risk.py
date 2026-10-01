"""One capital cap and one event exposure across Kalshi and Polymarket US.

Equivalent markets share an event when the normalized title and the
resolution text match. A partial title overlap with the same resolution
is flagged and is not merged: an uncertain pair does not offset.

YES is positive exposure. NO is negative. One kill calls cancel on both
venues. This module does not open a socket.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


def _norm(text: str) -> str:
    return " ".join(
        "".join(ch if ch.isalnum() else " " for ch in (text or "").lower()).split()
    )


def _words(text: str) -> set[str]:
    return set(_norm(text).split())


def _jaccard(a: str, b: str) -> float:
    left, right = _words(a), _words(b)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


@dataclass(frozen=True)
class Listing:
    venue: str
    market_id: str
    title: str
    resolution: str


@dataclass(frozen=True)
class Position:
    venue: str
    market_id: str
    side: str          # "yes" or "no"
    usd: float


@dataclass(frozen=True)
class UncertainMatch:
    left: Listing
    right: Listing
    overlap: float


def match_markets(listings: list[Listing], *, overlap: float = 0.5
                  ) -> tuple[dict[tuple[str, str], str], list[UncertainMatch]]:
    """Return ``(venue, market) -> event id`` for certain matches, plus flags.

    A market with no certain partner keeps its own id ``venue:market_id``
    only once a position asks. This map includes an entry for every listing
    that shares an exact title and resolution with at least one other listing.
    """
    buckets: dict[tuple[str, str], list[Listing]] = defaultdict(list)
    for item in listings:
        buckets[(_norm(item.title), _norm(item.resolution))].append(item)
    event_of: dict[tuple[str, str], str] = {}
    for (title, resolution), items in buckets.items():
        if len(items) < 2:
            continue
        event_id = f"{title}|{resolution}"
        for item in items:
            event_of[(item.venue, item.market_id)] = event_id
    uncertain: list[UncertainMatch] = []
    by_resolution: dict[str, list[Listing]] = defaultdict(list)
    for item in listings:
        by_resolution[_norm(item.resolution)].append(item)
    for group in by_resolution.values():
        for i, left in enumerate(group):
            for right in group[i + 1:]:
                if _norm(left.title) == _norm(right.title):
                    continue
                score = _jaccard(left.title, right.title)
                if score >= overlap:
                    uncertain.append(UncertainMatch(left, right, score))
    return event_of, uncertain


def _event_id(position: Position, event_of: dict[tuple[str, str], str]) -> str:
    return event_of.get(
        (position.venue, position.market_id),
        f"{position.venue}:{position.market_id}",
    )


def net_exposure(positions: list[Position],
                 event_of: dict[tuple[str, str], str]) -> dict[str, float]:
    nets: dict[str, float] = defaultdict(float)
    for position in positions:
        sign = 1.0 if position.side == "yes" else -1.0
        nets[_event_id(position, event_of)] += sign * float(position.usd)
    return dict(nets)


def limit_breaches(*, positions: list[Position],
                   event_of: dict[tuple[str, str], str],
                   capital_usd: dict[str, float],
                   global_cap_usd: float,
                   event_limit_usd: float) -> list[str]:
    reasons = []
    if sum(float(v) for v in capital_usd.values()) > float(global_cap_usd) + 1e-9:
        reasons.append("global_capital")
    for event_id, net in net_exposure(positions, event_of).items():
        if abs(net) > float(event_limit_usd) + 1e-9:
            reasons.append(event_id)
    return reasons


class CrossKill:
    """One switch. Both venues are cancelled, in paper or on a stub."""

    def __init__(self, kalshi, pm, kalshi_orders: list[tuple[str, str]]) -> None:
        self.kalshi = kalshi
        self.pm = pm
        self.kalshi_orders = list(kalshi_orders)
        self.killed = False
        self.reason = ""

    def trip(self, reason: str) -> None:
        self.killed = True
        self.reason = reason
        self.kalshi.cancel_all(self.kalshi_orders)
        self.pm.cancel_all()
