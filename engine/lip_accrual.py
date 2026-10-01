"""Per-second Kalshi LIP accrual from the websocket book and our order state.

Rules scored here are the Help Center text retrieved 2026-10-01:
https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program

A snapshot's reference on each side is the first bid where cumulative size
reaches Target Size / 5. Size at or above that price scores in full. Each
tick below multiplies by the discount factor (0.5 halves the credit). The
snapshot score is the YES share plus the NO share, so the whole book is
worth 2 and our fraction of the snapshot is that sum divided by 2. A
snapshot pays nothing when either side is under Target Size. Those seconds
are forfeited: they contribute zero and they stay in the period length, which
is how the published example scales the pool by valid/total snapshots.

The Help Center article does not publish a top-N participant truncation.
Orders deeper than the target-reaching level already earn nothing (the
cutoff in ``score_snapshot``). When a program row includes
``max_reward_per_account`` (centi-cents on GET /incentive_programs), that
dollar cap is applied to the period total before the cent floor and the $1
minimum. The minimum applies once to the period, not to each second.

Our size is the resting remainder on our orders. A fill reduces that
remainder, so filled size stops earning. The same remainder is removed from
the websocket book before it is added back, so a level that already echoes
our order is not counted twice and is not left inside the competitor total.

Sequence gaps mark the book stale and request a resync. The live socket
path that actually resubscribes is ``KalshiWS._on_seq_gap``. This module
does not open a socket. Seconds with a stale, empty, or off-grid book are
``unknown`` and are omitted from the dollar sum. They are not filled in
with the previous share. A second the caller skips is ``missed`` and is
also omitted. Seconds that only partly overlap the program window are
``boundary`` and are omitted. Programs whose start is before 2026-07-30
are tagged ``pre_2026_07_30`` and are not scored with this formula.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from typing import Optional

from engine.lip_scorer import (
    OurQuotes, ProgramParams, score_snapshot, snapshot_share,
)
from execution.kalshi_ws import BookLevel, BookState, KalshiWS

# Current two-sided LIP formula, Help Center as retrieved 2026-10-01.
# Programs that start earlier used the 28 February 2026 terms. This module
# does not invent that older formula.
RULE_2026_07_30 = datetime(2026, 7, 30, tzinfo=timezone.utc)
RULE_CURRENT = "2026_07_30"
RULE_PRE = "pre_2026_07_30"
RULE_UNDATED = "undated"


def rule_version(start_ts: Optional[float]) -> str:
    if start_ts is None:
        return RULE_UNDATED
    start = datetime.fromtimestamp(float(start_ts), timezone.utc)
    if start >= RULE_2026_07_30:
        return RULE_CURRENT
    return RULE_PRE


def max_reward_usd_from_centi_cents(raw: Optional[int]) -> Optional[Decimal]:
    """GET /incentive_programs max_reward_per_account is centi-cents."""
    if raw is None:
        return None
    if type(raw) is not int or raw < 0:
        raise ValueError("max_reward_per_account must be a non-negative int or absent")
    return Decimal(raw) / Decimal(10000)


@dataclass(frozen=True)
class RestingOrder:
    """One of our orders. ``remaining`` is the size still earning.

    ``in_book`` is the size of this order still visible on the websocket.
    It defaults to ``remaining``. After a fill, pass the pre-fill size as
    ``in_book`` until the book delta arrives so those contracts are not
    left in the competitor total.
    """
    side: str
    price_cents: int
    remaining: float
    in_book: Optional[float] = None


def book_for_score(book: BookState, orders: list[RestingOrder]) -> tuple[BookState, OurQuotes]:
    """Competitor book plus our resting size, each contract once."""
    yes = {lvl.price_cents: lvl.size for lvl in book.yes_bids}
    no = {lvl.price_cents: lvl.size for lvl in book.no_bids}
    our_yes: dict[int, float] = {}
    our_no: dict[int, float] = {}
    for order in orders:
        if order.side not in ("yes", "no"):
            raise ValueError(f"resting side must be yes or no, got {order.side!r}")
        if order.remaining < 0 or (order.in_book is not None and order.in_book < 0):
            raise ValueError("resting size cannot be negative")
        levels = yes if order.side == "yes" else no
        ours = our_yes if order.side == "yes" else our_no
        visible = order.remaining if order.in_book is None else order.in_book
        levels[order.price_cents] = max(0.0, levels.get(order.price_cents, 0.0) - visible)
        if order.remaining > 0:
            levels[order.price_cents] = levels.get(order.price_cents, 0.0) + order.remaining
            ours[order.price_cents] = ours.get(order.price_cents, 0.0) + order.remaining

    def pack(levels: dict[int, float]) -> list[BookLevel]:
        return [BookLevel(price, size) for price, size in sorted(levels.items(), reverse=True)
                if size > 1e-9]

    scored = BookState(
        market_ticker=book.market_ticker,
        yes_bids=pack(yes),
        no_bids=pack(no),
        snapshot_count=book.snapshot_count,
        stale=book.stale,
        unsupported_grid=book.unsupported_grid,
    )
    quotes = OurQuotes(
        yes_bids=[BookLevel(p, s) for p, s in sorted(our_yes.items(), reverse=True)],
        no_bids=[BookLevel(p, s) for p, s in sorted(our_no.items(), reverse=True)],
    )
    return scored, quotes


class ScoringBook(KalshiWS):
    """Snapshot + delta book for one market. No socket.

    A sequence gap marks the book stale and sets ``needs_resync``. The next
    snapshot clears both. Deltas received while stale are dropped.
    """

    def __init__(self, ticker: str):
        super().__init__(
            url="wss://demo-api.kalshi.co/trade-api/ws/v2",
            api_key="paper",
            private_key_path="/nonexistent",
        )
        self.market = ticker
        self.books[ticker] = BookState(market_ticker=ticker)
        self.needs_resync = False

    def _load_key(self) -> None:
        self._private_key = None

    @property
    def book(self) -> BookState:
        return self.books[self.market]

    def note_disconnect(self) -> None:
        book = self.book
        if not book.stale:
            book.stale_since_ts = 0.0
        book.stale = True
        book.stale_reason = "disconnect"
        self.needs_resync = True
        self._last_seq.clear()

    def _mark_gap(self) -> None:
        book = self.book
        book.gap_count += 1
        if not book.stale:
            book.stale_since_ts = 0.0
        book.stale = True
        book.stale_reason = "seq_gap"
        self.needs_resync = True

    def apply_message(self, msg: dict) -> str:
        mtype = msg.get("type", "")
        body = msg.get("msg") or {}
        ticker = body.get("market_ticker") or self.market
        if ticker != self.market:
            return "ignored"
        sid = msg.get("sid")
        seq = msg.get("seq")
        book = self.book
        if mtype == "orderbook_snapshot":
            self._seq_reset(sid, ticker, seq)
            if sid is not None:
                self._sid_to_tickers[sid] = [ticker]
            self._apply_snapshot(book, body)
            book.sid = sid if sid is not None else book.sid
            book.last_seq = int(seq) if seq is not None else 0
            self.needs_resync = False
            return "snapshot"
        if mtype == "orderbook_delta":
            if sid is not None and book.sid is not None and sid != book.sid:
                return "sid_mismatch"
            verdict = self._seq_check(sid, ticker, seq)
            if verdict == "dup":
                return "dup"
            if verdict == "gap":
                self._mark_gap()
                return "gap"
            if book.stale or book.snapshot_count <= 0:
                return "stale_drop"
            if self._apply_delta(book, body):
                book.last_seq = int(seq) if seq is not None else book.last_seq
                return "delta"
            return "ignored"
        return "ignored"


@dataclass
class SecondMark:
    second: int
    status: str
    counted: bool
    share: Optional[float] = None
    snapshot_score: float = 0.0
    intra_second: bool = False


def period_payout(raw: Decimal, max_reward_usd: Optional[Decimal] = None) -> Decimal:
    """Cap, floor to the cent, then drop a period total under $1."""
    if raw <= 0:
        return Decimal(0)
    capped = raw if max_reward_usd is None else min(raw, max_reward_usd)
    cents = (capped * 100).to_integral_value(rounding=ROUND_FLOOR)
    floored = cents / Decimal(100)
    if floored < 1:
        return Decimal(0)
    return floored


def payout_from_shares(
    shares: list[Optional[Decimal]],
    *,
    period_reward_usd: Decimal,
    period_seconds: int,
    max_reward_usd: Optional[Decimal] = None,
) -> tuple[Decimal, Decimal]:
    """Dollars from per-second shares. ``None`` is an unknown second.

    The rate is the full-window pool divided by ``period_seconds``. Unknown
    seconds add nothing. A zero share is a forfeited snapshot and stays in
    the window. The $1 floor runs on the sum.
    """
    if period_seconds <= 0:
        raise ValueError("period_seconds must be positive")
    pool = Decimal(period_reward_usd)
    raw = Decimal(0)
    for share in shares:
        if share is None:
            continue
        if share < 0:
            raise ValueError("share cannot be negative")
        raw += Decimal(share) * pool / Decimal(period_seconds)
    return raw, period_payout(raw, max_reward_usd)


@dataclass
class PeriodEstimate:
    market: str
    program_id: str
    series: str
    rule_version: str
    known_seconds: int
    unknown_seconds: int
    forfeited_seconds: int
    out_of_program_seconds: int
    boundary_seconds: int
    unsupported_rule_seconds: int
    intra_second_seconds: int
    sum_snapshot_score: str
    raw_usd: str
    estimated_usd: str
    max_reward_usd: Optional[str]


class SecondAccrual:
    """Score one wall-clock second at a time for one program window."""

    def __init__(self, params: ProgramParams, *, series: str = "",
                 max_reward_usd: Optional[Decimal] = None):
        if params.start_ts is None or params.end_ts is None:
            raise ValueError("accrual requires program start_ts and end_ts")
        self.params = params
        self.series = series
        self.max_reward_usd = max_reward_usd
        self.book = ScoringBook(params.market_ticker)
        self.resting: list[RestingOrder] = []
        self.marks: list[SecondMark] = []
        self.rule = rule_version(params.start_ts)
        self._next: Optional[int] = None
        self._gens: list[int] = []
        self.late_messages = 0

    def set_resting(self, orders: list[RestingOrder]) -> None:
        self.resting = list(orders)

    def on_message(self, msg: dict, ts: float) -> str:
        second = int(ts)
        if self._next is not None and second < self._next:
            self.late_messages += 1
            return "late"
        verdict = self.book.apply_message(msg)
        if verdict in ("snapshot", "delta"):
            self._gens.append(self.book.book.snapshot_count + self.book.book.delta_count)
        return verdict

    def _mark(self, second: int, status: str, *, counted: bool,
              share: Optional[float] = None, snapshot_score: float = 0.0,
              intra: bool = False) -> SecondMark:
        mark = SecondMark(second, status, counted, share, snapshot_score, intra)
        self.marks.append(mark)
        return mark

    def score_second(self, second: int) -> SecondMark:
        if self._next is not None and second < self._next:
            raise ValueError(f"second {second} is already closed")
        if self._next is not None and second > self._next:
            missed = second
            while self._next < missed:
                self._mark(self._next, "missed", counted=False)
                self._next += 1
        intra = len(self._gens) > 1
        self._gens.clear()
        mark = self._score_one(second, intra)
        self._next = second + 1
        return mark

    def _score_one(self, second: int, intra: bool) -> SecondMark:
        start = float(self.params.start_ts)
        end = float(self.params.end_ts)
        if self.rule != RULE_CURRENT:
            return self._mark(second, "unsupported_rule", counted=False, intra=intra)
        fully_inside = start <= second and (second + 1) <= end
        overlaps = start < (second + 1) and second < end
        if not fully_inside:
            status = "boundary" if overlaps else "out_of_program"
            return self._mark(second, status, counted=False, intra=intra)
        if not self.book.book.is_usable():
            return self._mark(second, "unknown", counted=False, intra=intra)
        scored, ours = book_for_score(self.book.book, self.resting)
        snap = score_snapshot(scored, ours, self.params)
        share = snapshot_share(snap)
        status = "scored" if share > 0 else "forfeited"
        return self._mark(
            second, status, counted=True, share=share,
            snapshot_score=snap.our_total_score, intra=intra,
        )

    def raw_usd(self) -> Decimal:
        pool = Decimal(str(self.params.period_reward_usd))
        length = Decimal(str(self.params.period_seconds))
        if length <= 0:
            return Decimal(0)
        raw = Decimal(0)
        for mark in self.marks:
            if not mark.counted or mark.share is None:
                continue
            raw += Decimal(str(mark.share)) * pool / length
        return raw

    def payable_usd(self) -> Decimal:
        return period_payout(self.raw_usd(), self.max_reward_usd)

    def estimate(self) -> PeriodEstimate:
        def n(status: str) -> int:
            return sum(1 for mark in self.marks if mark.status == status)

        known = sum(1 for mark in self.marks if mark.counted)
        unknown = sum(1 for mark in self.marks if mark.status in ("unknown", "missed"))
        score = sum((Decimal(str(mark.snapshot_score)) for mark in self.marks if mark.counted),
                    Decimal(0))
        raw = self.raw_usd()
        payable = self.payable_usd()
        return PeriodEstimate(
            market=self.params.market_ticker,
            program_id=self.params.program_id,
            series=self.series,
            rule_version=self.rule,
            known_seconds=known,
            unknown_seconds=unknown,
            forfeited_seconds=n("forfeited"),
            out_of_program_seconds=n("out_of_program"),
            boundary_seconds=n("boundary"),
            unsupported_rule_seconds=n("unsupported_rule"),
            intra_second_seconds=sum(1 for mark in self.marks if mark.intra_second),
            sum_snapshot_score=format(score, "f"),
            raw_usd=format(raw, "f"),
            estimated_usd=format(payable, "f"),
            max_reward_usd=None if self.max_reward_usd is None else format(self.max_reward_usd, "f"),
        )
