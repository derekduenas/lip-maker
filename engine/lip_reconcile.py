"""Match a Kalshi liquidity credit to our period estimate.

Payout endpoint, docs.kalshi.com catalog retrieved 2026-10-01
--------------------------------------------------------------
No trade-API route returns our incentive award or a per-period LIP score.

* ``GET /incentive_programs`` (also ``/trade-api/v2/incentive_programs``)
  returns the pool (``period_reward``, centi-cents), a program-level
  ``paid_out`` boolean, and optional ``max_reward_per_account`` (centi-cents).
  ``paid_out`` means the program was paid. It is not our dollar amount.
* ``GET /portfolio/settlements`` is contract resolution (yes/no cost and
  fees), not a liquidity award.
* ``GET /portfolio/fills`` is trades.
* ``GET /portfolio/balance`` is a cash snapshot. An untagged balance change
  is not a confirmed reward. ``infer_reward_credits`` may label a positive
  residual (balance change minus fills, settlements, and deposits) as an
  inferred credit. That residual is not added to ``PAID_SOURCES``.
* Deposits, withdrawals, and transfers are cash movements, not LIP credits.
* FIX collateral tag PAYOUT in the same catalog is a contract settlement
  payout, not an incentive score.

Until Kalshi publishes a per-user award route, a confirmed credit counts
only when the caller tags it ``liquidity_reward`` and names a source in
``engine.reward_provenance.PAID_SOURCES``. A separate residual path can
feed the series multiplier and is marked inferred. This module does not
call the network.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from engine.reward_provenance import PAID_SOURCES

PAYOUT_ENDPOINT: Optional[str] = None
INFERRED_SOURCE = "inferred_balance"
PAYOUT_ENDPOINT_FINDING = (
    "No per-user liquidity payout endpoint in the Kalshi Trade API catalog "
    "fetched from docs.kalshi.com on 2026-10-01. GET /incentive_programs "
    "returns the pool, a program-level paid_out flag, and optional "
    "max_reward_per_account. GET /portfolio/settlements, /portfolio/fills, "
    "and /portfolio/balance are not an incentive score. A confirmed credit is "
    "accepted only when it is tagged liquidity_reward with a PAID_SOURCES "
    "source. infer_reward_credits labels an unexplained positive residual "
    "as inferred_balance and does not add that source to PAID_SOURCES."
)


@dataclass(frozen=True)
class EstimateRow:
    market: str
    program_id: str
    series: str
    estimated_usd: Decimal
    period_start: str = ""


@dataclass(frozen=True)
class Credit:
    market: str
    program_id: str
    amount_usd: Decimal
    source: str
    period_start: str = ""


@dataclass(frozen=True)
class Match:
    market: str
    program_id: str
    series: str
    estimated_usd: str
    paid_usd: str
    ratio: Optional[str]


def program_metadata(row: dict) -> dict:
    """Incentive-program fields are the pool and a paid flag, not our award."""
    return {
        "market": row.get("market_ticker") or "",
        "program_id": row.get("id") or "",
        "paid_out": bool(row.get("paid_out", False)),
        "payout_usd": None,
        "reason": "incentive_programs has no per-user amount",
    }


def _money(value) -> Decimal:
    amount = Decimal(str(value))
    if amount < 0 or amount != amount:
        raise ValueError("reward amount must be a non-negative number")
    return amount


def credits_from_ledger(entries: list[dict]) -> tuple[list[Credit], list[dict]]:
    """Keep tagged liquidity rewards. Everything else is rejected.

    A balance delta, a settlement row, or ``paid_out: true`` on a program
    does not become a credit.
    """
    accepted: list[Credit] = []
    rejected: list[dict] = []
    for entry in entries:
        kind = str(entry.get("kind") or "")
        if kind != "liquidity_reward":
            rejected.append({"entry": entry, "reason": "not_a_liquidity_reward"})
            continue
        source = entry.get("source")
        if source not in PAID_SOURCES:
            rejected.append({"entry": entry, "reason": "untagged_source"})
            continue
        market = str(entry.get("market") or entry.get("market_ticker") or "")
        program_id = str(entry.get("program_id") or "")
        if not market or not program_id:
            rejected.append({"entry": entry, "reason": "missing_market_or_program"})
            continue
        try:
            amount = _money(entry.get("amount_usd"))
        except (ValueError, ArithmeticError):
            rejected.append({"entry": entry, "reason": "bad_amount"})
            continue
        accepted.append(Credit(
            market=market,
            program_id=program_id,
            amount_usd=amount,
            source=str(source),
            period_start=str(entry.get("period_start") or ""),
        ))
    return accepted, rejected


def reconcile(estimates: list[EstimateRow], credits: list[Credit]) -> dict:
    """Pair each credit with the estimate for the same market and program.

    ``ratio`` is paid / estimate when the estimate is positive. Two estimates
    for one key are ambiguous and are not matched. Credits for one key sum.
    """
    by_key: dict[tuple[str, str], list[EstimateRow]] = {}
    for row in estimates:
        by_key.setdefault((row.market, row.program_id), []).append(row)
    paid: dict[tuple[str, str], Decimal] = {}
    unmatched_credits: list[Credit] = []
    for credit in credits:
        key = (credit.market, credit.program_id)
        if key not in by_key:
            unmatched_credits.append(credit)
            continue
        paid[key] = paid.get(key, Decimal(0)) + credit.amount_usd

    matches: list[Match] = []
    ambiguous: list[tuple[str, str]] = []
    unmatched_estimates: list[EstimateRow] = []
    for key, rows in by_key.items():
        if key not in paid:
            unmatched_estimates.extend(rows)
            continue
        if len(rows) != 1:
            ambiguous.append(key)
            continue
        row = rows[0]
        amount = paid[key]
        ratio = None
        if row.estimated_usd > 0:
            ratio = format(amount / row.estimated_usd, "f")
        matches.append(Match(
            market=row.market,
            program_id=row.program_id,
            series=row.series,
            estimated_usd=format(row.estimated_usd, "f"),
            paid_usd=format(amount, "f"),
            ratio=ratio,
        ))
    return {
        "payout_endpoint": PAYOUT_ENDPOINT,
        "matches": matches,
        "unmatched_estimates": unmatched_estimates,
        "unmatched_credits": unmatched_credits,
        "ambiguous": ambiguous,
    }


def _signed(value) -> Decimal:
    return Decimal(str(value))


def infer_reward_credits(*, balance_delta_usd, fills_cash_usd=0,
                         settlements_usd=0, deposits_usd=0,
                         shares: dict | None = None,
                         series_by_market: dict | None = None,
                         program_by_market: dict | None = None) -> dict:
    """Attribute a balance residual that fills, settlements, and deposits do not explain.

    Every input is a signed cashflow in the same window: a fill that spent
    premium is negative, a deposit is positive. The residual is

        balance_delta − fills_cash − settlements − deposits

    A residual at or below zero is not a reward. A positive residual is
    split by ``shares`` (per-market estimated dollars) and marked inferred.
    ``INFERRED_SOURCE`` is not a member of ``PAID_SOURCES``.
    """
    if INFERRED_SOURCE in PAID_SOURCES:
        raise RuntimeError("inferred rewards must stay out of PAID_SOURCES")
    residual = (_signed(balance_delta_usd) - _signed(fills_cash_usd)
                - _signed(settlements_usd) - _signed(deposits_usd))
    out = {
        "source": INFERRED_SOURCE,
        "inferred": True,
        "confirmed": False,
        "residual_usd": format(residual, "f"),
        "credits": [],
        "unattributed_usd": "0",
    }
    if residual <= 0:
        return out
    weights: dict[str, Decimal] = {}
    for market, weight in (shares or {}).items():
        amount = _signed(weight)
        if amount > 0:
            weights[str(market)] = amount
    total = sum(weights.values(), Decimal(0))
    if total <= 0:
        out["unattributed_usd"] = format(residual, "f")
        return out
    series_by_market = series_by_market or {}
    program_by_market = program_by_market or {}
    credits = []
    for market, weight in weights.items():
        amount = residual * weight / total
        credits.append({
            "market": market,
            "program_id": str(program_by_market.get(market) or market),
            "series": str(series_by_market.get(market) or market.split("-", 1)[0]),
            "amount_usd": format(amount, "f"),
            "source": INFERRED_SOURCE,
            "inferred": True,
            "kind": "inferred_reward",
        })
    out["credits"] = credits
    return out
