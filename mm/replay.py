"""Replay a recording through the same paper-fill model the runner uses.

``execution.paper_fills.PaperFillSimulator`` is the fill rule: we are last
in queue at our price, trades before activation do not fill us, and only a
print at our price on the opposing taker side consumes us. Replay does not
have a second, kinder fill model.

P&L marks a fill to a later settlement if the recording contains one, and
otherwise to the last YES mid in the file. Fees and rebates come from
``mm.accounting``. Estimated incentive dollars are reported beside cash,
never inside it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from execution.paper_fills import PaperFillSimulator
from mm.accounting import Books, kalshi_fee_usd, pm_us_maker_rebate_usd
from mm.recorder import read_records

ZERO = Decimal("0")


class _Level:
    def __init__(self, price_cents: int, size: float) -> None:
        self.price_cents = int(price_cents)
        self.size = float(size)


class _Book:
    def __init__(self, yes: list[tuple[int, float]], no: list[tuple[int, float]]) -> None:
        self.yes_bids = [_Level(p, s) for p, s in yes]
        self.no_bids = [_Level(p, s) for p, s in no]


@dataclass
class ReplayResult:
    fills: list[dict] = field(default_factory=list)
    cash_pnl_usd: Decimal = ZERO
    estimated_reward_usd: Decimal = ZERO
    fees_usd: Decimal = ZERO
    rebates_usd: Decimal = ZERO
    books: Books = field(default_factory=Books)
    last_mid: dict = field(default_factory=dict)
    settlements: dict = field(default_factory=dict)


def replay(path: str, *, fee_type: str = "quadratic",
           venue: str = "kalshi", latency_ms: float = 0.0) -> ReplayResult:
    """Walk a JSONL recording and return paper P&L.

    Quote records become resting paper orders. Trade records are fed to
    ``PaperFillSimulator``. A ``settlement`` record is ``{"market", "yes_cents"}``.
    An ``estimate`` record adds estimated reward dollars and does not change cash.
    """
    sim = PaperFillSimulator(latency_ms=latency_ms)
    result = ReplayResult()
    books_seen: dict[str, _Book] = {}
    for row in read_records(path):
        kind = row.get("kind")
        if kind == "book":
            market = row["market"]
            books_seen[market] = _Book(
                [(int(row["yes_bid"]), float(row.get("yes_size") or 0))],
                [(int(row["no_bid"]), float(row.get("no_size") or 0))],
            )
            result.last_mid[market] = (int(row["yes_bid"]) + (100 - int(row["no_bid"]))) / 2.0
        elif kind == "quote":
            market = row["market"]
            book = books_seen.get(market)
            sim.track(
                order_id=row["order_id"], market_ticker=market, side=row["side"],
                price_cents=int(row["price_cents"]), size=float(row["size"]),
                book=book, now=float(row.get("ts") or 0),
            )
        elif kind == "trade":
            fills = sim.apply_trades([row["trade"]])
            for fill in fills:
                _account_fill(result, fill, fee_type=fee_type, venue=venue)
        elif kind == "settlement":
            result.settlements[row["market"]] = int(row["yes_cents"])
        elif kind == "estimate":
            b = result.books.book(row["market"])
            b.add_estimate(Decimal(str(row["usd"])))
            result.estimated_reward_usd += Decimal(str(row["usd"]))
    _mark(result)
    result.cash_pnl_usd = result.books.cash_total()
    return result


def _account_fill(result: ReplayResult, fill: dict, *, fee_type: str, venue: str) -> None:
    result.fills.append(fill)
    px = int(fill["price_cents"])
    n = fill["count"]
    book = result.books.book(fill["market_ticker"])
    if venue == "pmus":
        rebate = pm_us_maker_rebate_usd(px, n)
        fee = ZERO
        book.rebates_usd += rebate
        result.rebates_usd += rebate
    else:
        fee = kalshi_fee_usd(px, n, fee_type=fee_type, is_taker=False)
        book.fees_usd += fee
        result.fees_usd += fee
    fill["fee_usd"] = str(fee)


def _mark(result: ReplayResult) -> None:
    """Mark open inventory to settlement if we have one, else to the last mid.

    A YES buy profits when the mark is above the fill. A NO buy profits when
    the YES mark is below ``100 - fill`` (we own NO, worth 100 − YES).
    """
    inv: dict[tuple[str, str], list[tuple[float, int]]] = {}
    for fill in result.fills:
        key = (fill["market_ticker"], fill["side"])
        inv.setdefault(key, []).append((float(fill["count"]), int(fill["price_cents"])))
    for (market, side), lots in inv.items():
        if market in result.settlements:
            yes_mark = float(result.settlements[market])
        else:
            yes_mark = result.last_mid.get(market)
        if yes_mark is None:
            continue
        book = result.books.book(market)
        for count, px in lots:
            if side == "yes":
                pnl = Decimal(str(count)) * (Decimal(str(yes_mark)) - Decimal(px)) / Decimal(100)
            else:
                no_mark = Decimal(100) - Decimal(str(yes_mark))
                pnl = Decimal(str(count)) * (no_mark - Decimal(px)) / Decimal(100)
            book.realized_usd += pnl


def _cli(argv=None) -> int:
    """``python -m mm.replay bench ...`` -> Patch 19 replay bench (mm.replay_bench)."""
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "bench":
        argv = argv[1:]
    from mm.replay_bench import main
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(_cli())
