"""Paid vs estimated LIP rewards (paper; read-only reconciliation).

Provenance rule (engine/reward_provenance.py): a reward figure is PAID only when it
comes from an independent record (PAID_SOURCES). Our own accrual is an ESTIMATE and
is never written back as paid, never enters a calibration as actual, and never moves
a paid total. This module only COMPARES the two:

* estimates: one row per archived program period (``RunLoop.period_estimates``):
  the payable estimate after the $1 floor, keyed by the program's real id;
* credits: operator-supplied independent records (``LIP_REWARD_CREDITS_FILE``, JSONL,
  same keys as ``engine.lip_reconcile.credits_from_ledger``): kind=liquidity_reward,
  source in PAID_SOURCES, market, program_id, amount_usd, optional period_start.

No Kalshi API returns our award (engine/lip_reconcile.py documents this), so the
credit file is the door, as with tools/reward_payments.py.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from engine.lip_reconcile import EstimateRow, credits_from_ledger

MIN_MATCHES = 10          # matched periods before the measured ratio is trusted
MIN_PAID_DAYS = 3         # distinct paid period dates
MIN_HAIRCUT = 0.2         # never trust more than 80% of the estimate
LAG_S = 3 * 86400.0       # an estimate archived less than this ago is not yet due a credit
LABEL = ("estimates are never paid money; paid = independent records only "
         "(engine.reward_provenance.PAID_SOURCES)")


def _fmt(value: Decimal) -> str:
    return format(value, "f")


def _day(text) -> str:
    return str(text or "")[:10]


def reconcile_periods(estimates: list, entries: list, *, now: float | None = None, lag_s: float = 0.0) -> dict:
    """Paid vs estimated over archived periods.

    Matching is per period: a credit with a ``period_start`` pairs with the estimate of the same
    (market, program_id, day); a credit without one pairs only when exactly one estimate has that
    (market, program_id), else it is ambiguous and counts for nothing. Credits for one estimate sum.

    The ratio is paid / (all DUE estimates): an estimate that was never paid stays in the
    denominator (a ratio over the survivors is biased upward). With ``now`` and ``lag_s`` an
    estimate archived less than ``lag_s`` ago is not yet due (credits arrive late) unless it
    already has a credit. ``paid_days`` counts distinct days of MATCHED credits only."""
    accepted, rejected = credits_from_ledger(list(entries))
    rows = []                                    # (EstimateRow, archived_ts or None)
    for e in estimates:
        try:
            est = Decimal(str(e["estimated_usd"]))
            if not est.is_finite() or est < 0:
                continue
            rows.append((EstimateRow(str(e["market"]), str(e["program_id"]), str(e.get("series") or ""),
                                     est, str(e.get("period_start") or "")), e.get("ts")))
        except (KeyError, ArithmeticError, ValueError):
            continue
    by_key: dict = {}
    for i, (row, _ts) in enumerate(rows):
        by_key.setdefault((row.market, row.program_id), []).append(i)
    paid = [Decimal(0)] * len(rows)
    got = [False] * len(rows)
    unmatched_credits, ambiguous_n, matched_days = [], 0, set()
    for c in accepted:
        idx = by_key.get((c.market, c.program_id), [])
        if c.period_start:
            hit = [i for i in idx if _day(rows[i][0].period_start) == _day(c.period_start)]
        else:
            hit = idx if len(idx) == 1 else []
            if len(idx) > 1:
                ambiguous_n += 1
                continue
        if len(hit) != 1:
            unmatched_credits.append(c)
            continue
        paid[hit[0]] += c.amount_usd
        got[hit[0]] = True
        matched_days.add(_day(c.period_start) or _day(rows[hit[0]][0].period_start))
    matched_i = [i for i in range(len(rows)) if got[i]]

    def due(i: int) -> bool:
        ts = rows[i][1]
        if got[i] or now is None or lag_s <= 0 or ts is None:
            return True
        try:
            return float(now) - float(ts) >= float(lag_s)
        except (TypeError, ValueError):
            return True
    due_i = [i for i in range(len(rows)) if due(i)]
    paid_sum = sum((paid[i] for i in matched_i), Decimal(0))
    est_matched = sum((rows[i][0].estimated_usd for i in matched_i), Decimal(0))
    est_due = sum((rows[i][0].estimated_usd for i in due_i), Decimal(0))
    by_series: dict = {}
    for i in due_i:
        s = by_series.setdefault(rows[i][0].series or "?", [Decimal(0), Decimal(0), 0, 0])
        s[0] += paid[i]
        s[1] += rows[i][0].estimated_usd
        s[2] += 1 if got[i] else 0
        s[3] += 1
    reasons: dict = {}
    for r in rejected:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    unmatched_est = [i for i in due_i if not got[i]]
    return {
        "matched": len(matched_i), "due_estimates": len(due_i),
        "paid_usd": _fmt(paid_sum), "estimated_usd": _fmt(est_due), "matched_estimated_usd": _fmt(est_matched),
        "ratio": None if est_due <= 0 else _fmt(paid_sum / est_due),
        "by_series": {k: {"paid_usd": _fmt(v[0]), "estimated_usd": _fmt(v[1]), "matched": v[2], "due": v[3],
                          "ratio": None if v[1] <= 0 else _fmt(v[0] / v[1])} for k, v in sorted(by_series.items())},
        "unmatched_estimates": len(unmatched_est),
        "unmatched_estimated_usd": _fmt(sum((rows[i][0].estimated_usd for i in unmatched_est), Decimal(0))),
        "unmatched_credits": len(unmatched_credits),
        "unmatched_credit_usd": _fmt(sum((c.amount_usd for c in unmatched_credits), Decimal(0))),
        "ambiguous": ambiguous_n,
        "rejected": reasons, "paid_days": len({d for d in matched_days if d}),
    }


def effective_haircut(operator: float, measured) -> float:
    """The haircut the go/no-go uses: the measured one may only make it MORE conservative."""
    op = min(1.0, max(0.0, float(operator)))
    return op if measured is None else max(op, min(1.0, max(0.0, float(measured))))


def haircut_recommendation(rep: dict, *, min_matches: int = MIN_MATCHES, min_days: int = MIN_PAID_DAYS):
    """Reward haircut implied by the measured paid/estimated ratio (None = not enough data).

    haircut = 1 - ratio, clamped to [MIN_HAIRCUT, 1]: even a ratio above 1 never
    removes more than 80% of the conservatism."""
    if rep.get("ratio") is None or int(rep.get("matched") or 0) < min_matches \
            or int(rep.get("paid_days") or 0) < min_days:
        return None
    ratio = max(0.0, float(rep["ratio"]))
    return min(1.0, max(MIN_HAIRCUT, 1.0 - ratio))


def load_credit_file(path: str, ledger: list) -> dict:
    """Append new JSONL credit records to ``ledger`` (dedup by content hash).

    Records are stored as given plus ``entry_id``; whether they count as paid is
    decided later by ``credits_from_ledger`` (provenance), not here."""
    out = {"added": 0, "duplicates": 0, "bad_lines": 0, "error": None}
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        out["error"] = "unreadable"
        return out
    seen = {str(e.get("entry_id")) for e in ledger if isinstance(e, dict)}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            out["bad_lines"] += 1
            continue
        if not isinstance(row, dict):
            out["bad_lines"] += 1
            continue
        row.pop("entry_id", None)
        eid = hashlib.sha1(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()[:16]
        if eid in seen:
            out["duplicates"] += 1
            continue
        seen.add(eid)
        ledger.append(dict(row, entry_id=eid))
        out["added"] += 1
    del ledger[:-5000]
    return out
