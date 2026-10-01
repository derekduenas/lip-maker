"""One paper pass: select, size, quote, score, reconcile, allocate, risk.

Nothing here opens a socket or sends an order. Books come from a recording.
Live arming flags are not read and are not set.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from engine.lip_accrual import (
    RestingOrder, SecondAccrual, payout_from_shares,
)
from engine.lip_calibration import RatioObs, error_distribution, series_factors
from engine.lip_reconcile import Credit, EstimateRow, credits_from_ledger, reconcile
from engine.lip_scorer import ProgramParams
from mm.compound import MarketSample, posterior, reallocate
from mm.ops import skew_is_excessive
from mm.risk import RiskEngine
from mm.selector import KalshiMarket, allocate
from mm.unattended.optimize import optimize_sizes


def load_recording(path: str | Path) -> tuple[list[KalshiMarket], list[dict], list[dict]]:
    markets: list[KalshiMarket] = []
    books: list[dict] = []
    credits: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        kind = row.get("kind")
        if kind == "program":
            markets.append(_market_from_row(row))
        elif kind == "book":
            books.append(row)
        elif kind == "credit":
            credits.append(_credit_entry(row))
    return markets, books, credits


def _credit_entry(row: dict) -> dict:
    """A recording tags the ledger kind in ``entry_kind`` or ``reward_kind``.

    ``kind`` on the line is ``credit`` so the loader can find it. The
    reconciler only accepts ``kind=liquidity_reward``.
    """
    entry = dict(row)
    tagged = str(entry.get("entry_kind") or entry.get("reward_kind") or "")
    if tagged:
        entry["kind"] = tagged
    return entry


def _market_from_row(row: dict) -> KalshiMarket:
    yes = row.get("yes_bids")
    no = row.get("no_bids")
    if yes is None and row.get("yes_bid") is not None:
        yes = [(int(row["yes_bid"]), float(row.get("yes_size") or 0))]
    if no is None and row.get("no_bid") is not None:
        no = [(int(row["no_bid"]), float(row.get("no_size") or 0))]
    return _market(row, yes or [], no or [])


def _market(row, yes, no) -> KalshiMarket:
    return KalshiMarket(
        market=str(row["market"]),
        series=str(row.get("series") or str(row["market"]).split("-", 1)[0]),
        period_reward_usd=float(row.get("period_reward_usd") or 0),
        period_seconds=float(row.get("period_seconds") or 86400),
        seconds_left=float(row.get("seconds_left") or row.get("period_seconds") or 86400),
        discount_factor=float(row.get("discount_factor") or 0.5),
        target_size=float(row.get("target_size") or 100),
        yes_bids=[(int(p), float(s)) for p, s in yes],
        no_bids=[(int(p), float(s)) for p, s in no],
        days_to_settle=None if row.get("days_to_settle") is None else float(row["days_to_settle"]),
        exchange_index=None if row.get("exchange_index") is None else int(row["exchange_index"]),
        shard_cash_usd=float(row.get("shard_cash_usd") or 1e9),
    )


def _snap(seq: int, market: str, yes: list, no: list) -> dict:
    def levels(rows):
        return [[f"{int(p) / 100:.4f}", f"{float(s):.2f}"] for p, s in rows if float(s) > 0]
    return {
        "type": "orderbook_snapshot", "sid": 1, "seq": seq,
        "msg": {"market_ticker": market, "yes_dollars_fp": levels(yes),
                "no_dollars_fp": levels(no)},
    }


def _book_levels(row: dict, side: str) -> list[tuple[int, float]]:
    if row.get(f"{side}_bids"):
        return [(int(p), float(s)) for p, s in row[f"{side}_bids"]]
    bid = row.get(f"{side}_bid")
    if bid is None:
        return []
    return [(int(bid), float(row.get(f"{side}_size") or 0))]


def run_paper_cycle(markets: list[KalshiMarket], books: list[dict], *,
                    credits: list[dict] | None = None,
                    bankroll: float = 10_000.0,
                    chunk: float = 100.0) -> dict:
    """Paper cycle. ``books`` are recorded order books, not a live socket."""
    selection = allocate(
        markets, bankroll=bankroll, chunk=chunk, max_size=chunk,
        per_market_usd=bankroll, per_series_usd=bankroll, per_category_usd=bankroll,
    )
    sized = optimize_sizes(
        markets, bankroll=bankroll, per_market_usd=bankroll,
        per_event_usd=bankroll, total_usd=bankroll,
        sizes=(chunk,), markout_usd_per_contract=0.0,
    )
    quotes = [
        {"market": row.market, "size": row.size, "yes_cents": row.yes_cents,
         "no_cents": row.no_cents, "paper": True}
        for row in sized.chosen
    ]
    by_market = {m.market: m for m in markets}
    estimates = []
    scored_markets = []
    for market_name, group in _groups(books).items():
        market = by_market.get(market_name)
        chosen = next((q for q in quotes if q["market"] == market_name), None)
        if market is None or chosen is None:
            continue
        start = int(min(float(row["ts"]) for row in group))
        params = ProgramParams(
            market_ticker=market.market,
            target_size=market.target_size,
            discount_factor=market.discount_factor,
            period_reward_usd=market.period_reward_usd,
            program_id=market.market,
            period_seconds=float(market.period_seconds),
            start_ts=float(start),
            end_ts=float(start) + float(market.period_seconds),
        )
        accrual = SecondAccrual(params, series=market.series)
        accrual.set_resting([
            RestingOrder("yes", int(chosen["yes_cents"]), float(chosen["size"]), in_book=0),
            RestingOrder("no", int(chosen["no_cents"]), float(chosen["size"]), in_book=0),
        ])
        seq = 1
        for row in group:
            ts = float(row["ts"])
            exchange_ts = row.get("exchange_ts")
            if exchange_ts is not None and skew_is_excessive(ts, float(exchange_ts)):
                accrual.book.note_disconnect()
                accrual.score_second(int(ts))
                continue
            accrual.on_message(_snap(seq, market.market, _book_levels(row, "yes"),
                                      _book_levels(row, "no")), ts)
            accrual.score_second(int(ts))
            seq += 1
        est = accrual.estimate()
        estimates.append(EstimateRow(
            market.market, market.market, market.series, Decimal(est.estimated_usd),
        ))
        scored_markets.append(est.estimated_usd)
    accepted, rejected = credits_from_ledger(list(credits or []))
    matched = reconcile(estimates, accepted)
    observations = [
        RatioObs(row.series, Decimal(row.estimated_usd), Decimal(row.paid_usd))
        for row in matched["matches"]
        if row.ratio is not None
    ]
    factors = series_factors(observations)
    samples = []
    for row in estimates:
        observed = float(factors.get(row.series, 1.0))
        samples.append(MarketSample(
            market=row.market,
            observed_per_dollar=observed,
            prior_per_dollar=1.0,
            n=1 if row.series in factors else 0,
            previous_usd=0.0,
            venue="kalshi",
            series=row.series,
        ))
    next_sizes = reallocate(samples, equity=bankroll, peak=bankroll, fraction=0.25, min_sample=5)
    engine = RiskEngine()
    risk_rows = []
    kill = None
    for quote in quotes:
        add = Decimal(str((quote["yes_cents"] + quote["no_cents"]) / 100.0 * quote["size"]))
        decision = engine.check_quote(market=quote["market"], venue="kalshi", add_usd=add)
        risk_rows.append({"market": quote["market"], "allowed": decision.allowed,
                          "reason": decision.reason, "cancel_all": decision.cancel_all})
        if not decision.allowed:
            kill = {"reason": decision.reason, "cancel_all": decision.cancel_all, "paper": True}
            break
        engine.commit(quote["market"], "kalshi", add)
    estimated = sum((Decimal(row.estimated_usd) for row in estimates), Decimal(0))
    return {
        "paper": True,
        "live_armed": False,
        "stage": "risk",
        "markets": [m.market for m in markets],
        "selection": [row.market for row in selection.taken],
        "sizes": [row.market for row in sized.chosen],
        "quotes": quotes,
        "estimated_usd": format(estimated, "f"),
        "reconcile": {
            "matches": [m.ratio for m in matched["matches"]],
            "rejected": len(rejected),
        },
        "factors": factors,
        "error": error_distribution(observations),
        "next_usd": next_sizes,
        "posterior_at_no_samples": posterior(0.0, 1.0, 0),
        "risk": risk_rows,
        "kill": kill,
        "payout_from_shares_floor": format(
            payout_from_shares([Decimal("0.4")], period_reward_usd=Decimal("1"),
                               period_seconds=1)[1], "f"),
    }


def _groups(books: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in books:
        out.setdefault(str(row.get("market") or ""), []).append(row)
    return out


def run_recording(path: str | Path, *, bankroll: float = 10_000.0) -> dict:
    markets, books, credits = load_recording(path)
    return run_paper_cycle(markets, books, credits=credits, bankroll=bankroll)
