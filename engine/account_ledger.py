"""One account. One cash balance. Capital reserved when an order rests
(2026-09-21).

What was wrong
--------------
The system held three different, mutually inconsistent ideas of its own
capital and no idea at all of whether cash was available:

    config/settings.py:52      BANKROLL_USD default  $80
    execution/quote_manager.py `if self.paper: return 10_000.0`
    the stated goal            one shared $5,000 account

So `MAX_BANKROLL_SHARE_PCT = 0.50` was enforced against a $10,000 fiction in
exactly the mode we evaluate in — it permitted $5,000 of gross, i.e. 100% of
the real account. And nothing anywhere reserved capital: exposure was
inferred by summing resting orders, which answers "how much am I showing?"
and not "can I afford this order?". Two markets could each pass their own cap
while jointly exceeding the cash that exists.

What this does
--------------
A binary contract is fully collateralised: buying `n` contracts at `p` cents
costs exactly `n * p / 100` dollars and that is also the most you can lose.
So capital required by a resting order is knowable exactly, and reservation
is simple double-entry:

    reserve   on placement      cash unchanged, available falls
    release   on cancel/reject  reservation returns to available
    settle    on fill           reservation becomes inventory, cash falls

`available_usd = cash - reserved` is the only number allowed to authorise a
new order.

Shared with research
--------------------
Events are written through research.profit_ledger.ProfitLedger — the same
immutable, Decimal, program_id-keyed event store the offline analysis reads —
so the paper runner and replay report from one history rather than two
private accounting systems. Money is Decimal throughout; float dollars are a
rounding bug waiting to be discovered at settlement.
"""
from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings

_log = logging.getLogger(__name__)

ZERO = Decimal("0")
CENTS = Decimal("100")


def contract_cost_usd(price_cents, quantity) -> Decimal:
    """Collateral for `quantity` contracts bought at `price_cents`.

    Exact by construction: a binary pays $0 or $1, so the buyer posts the
    full premium and cannot lose more."""
    return (Decimal(str(price_cents)) / CENTS) * Decimal(str(quantity))


class InsufficientCapital(Exception):
    """The account cannot fund this order. Carries the shortfall."""

    def __init__(self, required: Decimal, available: Decimal, detail: str = ""):
        self.required = required
        self.available = available
        super().__init__(
            f"need ${required:.4f}, available ${available:.4f}"
            f"{' — ' + detail if detail else ''}")


@dataclass
class Reservation:
    order_id: str
    market: str
    program_id: str
    amount_usd: Decimal
    ts: float


@dataclass
class AccountState:
    cash_usd: Decimal
    reserved_usd: Decimal
    inventory_cost_usd: Decimal
    n_reservations: int

    @property
    def available_usd(self) -> Decimal:
        return self.cash_usd - self.reserved_usd

    @property
    def equity_usd(self) -> Decimal:
        """Cash plus what inventory cost. NOT what inventory is worth —
        marking to market needs a book and an exit-fee estimate, which
        research.profit_ledger.report() does properly and refuses to guess
        at when depth is missing."""
        return self.cash_usd + self.inventory_cost_usd


class AccountLedger:
    """Authoritative cash + reservation state for one account.

    Thread-safe: fills arrive on the WS loop while reconcile runs in executor
    threads, the same reason execution.quote_manager holds a state lock.
    """

    def __init__(self, *, opening_cash_usd=None, mode: str = "paper",
                 event_db_path: Optional[str] = None):
        if mode not in ("paper", "live"):
            raise ValueError("mode must be 'paper' or 'live'")
        self.mode = mode
        if opening_cash_usd is None:
            opening_cash_usd = getattr(settings, "ACCOUNT_OPENING_CASH_USD", 5000.0)
        self._opening = Decimal(str(opening_cash_usd))
        if self._opening < 0:
            raise ValueError("opening cash cannot be negative")
        self._cash = self._opening
        self._inventory_cost = ZERO
        self._reservations: dict[str, Reservation] = {}
        self._lock = threading.RLock()
        self._events = None
        if event_db_path:
            try:
                from research.profit_ledger import ProfitLedger
                self._events = ProfitLedger(event_db_path)
            except Exception as e:      # event log is an audit trail, not a
                _log.warning(f"event ledger unavailable ({e}); "
                             f"account state still enforced in memory")

    # ── state ────────────────────────────────────────────────────────
    def state(self) -> AccountState:
        with self._lock:
            return AccountState(self._cash, self._reserved_total(),
                                self._inventory_cost, len(self._reservations))

    def _reserved_total(self) -> Decimal:
        return sum((r.amount_usd for r in self._reservations.values()), ZERO)

    def available_usd(self) -> Decimal:
        with self._lock:
            return self._cash - self._reserved_total()

    def can_afford(self, price_cents, quantity) -> bool:
        return contract_cost_usd(price_cents, quantity) <= self.available_usd()

    def max_affordable_contracts(self, price_cents) -> int:
        """Largest whole lot fundable at this price right now."""
        px = Decimal(str(price_cents)) / CENTS
        if px <= 0:
            return 0
        return int(self.available_usd() / px)

    # ── reservations ─────────────────────────────────────────────────
    def reserve(self, order_id: str, *, market: str, program_id: str,
                price_cents, quantity, ts_ms: Optional[int] = None) -> Decimal:
        """Hold capital for an order about to rest. Raises InsufficientCapital.

        Idempotent by order_id: re-reserving the same id adjusts the existing
        hold rather than double-counting, so a retried placement cannot
        silently consume the account twice."""
        amount = contract_cost_usd(price_cents, quantity)
        with self._lock:
            prior = self._reservations.get(order_id)
            delta = amount - (prior.amount_usd if prior else ZERO)
            if delta > ZERO and delta > self._cash - self._reserved_total():
                raise InsufficientCapital(
                    delta, self._cash - self._reserved_total(),
                    f"{market} {quantity}@{price_cents}c")
            self._reservations[order_id] = Reservation(
                order_id, market, program_id, amount, ts_ms or 0)
            self._emit("reserve", market=market, program_id=program_id,
                       amount_usd=amount, ts_ms=ts_ms,
                       event_id=f"reserve:{order_id}:{amount}")
            return amount

    def release(self, order_id: str) -> Decimal:
        """Return an unfilled order's capital. Safe to call twice."""
        with self._lock:
            r = self._reservations.pop(order_id, None)
            return r.amount_usd if r else ZERO

    def on_fill(self, order_id: str, *, market: str, program_id: str,
                side: str, price_cents, quantity, fee_usd=ZERO,
                trade_id: str = "", ts_ms: Optional[int] = None) -> Decimal:
        """A reservation becomes inventory: cash leaves the account.

        Returns the cash spent. The caller must be idempotent about
        `trade_id` upstream (execution.quote_manager.apply_fill already is);
        when a trade_id is supplied the event store enforces it too.
        """
        cost = contract_cost_usd(price_cents, quantity)
        fee = Decimal(str(fee_usd))
        with self._lock:
            r = self._reservations.get(order_id)
            if r is not None:
                remaining = r.amount_usd - cost
                if remaining <= ZERO:
                    self._reservations.pop(order_id, None)
                else:
                    r.amount_usd = remaining      # partial fill: hold the rest
            self._cash -= (cost + fee)
            self._inventory_cost += cost
            self._emit("buy", market=market, program_id=program_id,
                       side=side, quantity=quantity,
                       price_usd=Decimal(str(price_cents)) / CENTS,
                       fee_usd=fee, ts_ms=ts_ms,
                       event_id=f"fill:{trade_id or order_id}")
            return cost + fee

    def on_settlement(self, *, market: str, program_id: str, side: str,
                      quantity, won: bool, cost_basis_usd=None,
                      ts_ms: Optional[int] = None) -> Decimal:
        """Inventory resolves: winners pay $1/contract, losers pay nothing."""
        qty = Decimal(str(quantity))
        proceeds = qty if won else ZERO
        basis = Decimal(str(cost_basis_usd)) if cost_basis_usd is not None else ZERO
        with self._lock:
            self._cash += proceeds
            self._inventory_cost -= basis
            if self._inventory_cost < ZERO:
                self._inventory_cost = ZERO
            self._emit("settlement", market=market, program_id=program_id,
                       side=side, quantity=qty,
                       price_usd=Decimal(1) if won else ZERO,
                       fee_usd=ZERO, ts_ms=ts_ms)
            return proceeds

    def credit_reward(self, amount_usd, *, market: str, program_id: str,
                      paid: bool, ts_ms: Optional[int] = None) -> None:
        """Reward money. `paid=False` records an ESTIMATE, which changes NO
        cash — consistent with engine.reward_provenance: only reconciled
        payments are money."""
        amt = Decimal(str(amount_usd))
        with self._lock:
            if paid:
                self._cash += amt
            self._emit("reward_credit" if paid else "reward_estimate",
                       market=market, program_id=program_id,
                       amount_usd=amt, ts_ms=ts_ms)

    # ── event trail ──────────────────────────────────────────────────
    def _emit(self, kind: str, *, market: str, program_id: str,
              ts_ms: Optional[int] = None, event_id: str = "", **fields) -> None:
        if self._events is None:
            return
        ev = {"kind": kind, "mode": self.mode, "market": market,
              "program_id": program_id, "source": "account_ledger",
              "ts_ms": int(ts_ms if ts_ms is not None else 0),
              "event_id": event_id or f"{kind}:{market}:{ts_ms}"}
        for k, v in fields.items():
            ev[k] = str(v) if isinstance(v, Decimal) else v
        try:
            self._events.append(ev)
        except Exception as e:
            # A rejected duplicate is the event store doing its job.
            _log.debug(f"account event not appended ({kind}): {e}")

    def summary(self) -> dict:
        s = self.state()
        return {"mode": self.mode, "opening_usd": str(self._opening),
                "cash_usd": str(s.cash_usd), "reserved_usd": str(s.reserved_usd),
                "available_usd": str(s.available_usd),
                "inventory_cost_usd": str(s.inventory_cost_usd),
                "equity_usd": str(s.equity_usd),
                "open_reservations": s.n_reservations}
