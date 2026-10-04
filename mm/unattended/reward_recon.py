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

from engine.lip_reconcile import EstimateRow, credits_from_ledger, reconcile

MIN_MATCHES = 10          # matched periods before the measured ratio is trusted
MIN_PAID_DAYS = 3         # distinct paid period dates
MIN_HAIRCUT = 0.2         # never trust more than 80% of the estimate
LABEL = ("estimates are never paid money; paid = independent records only "
         "(engine.reward_provenance.PAID_SOURCES)")


def _fmt(value: Decimal) -> str:
    return format(value, "f")


def reconcile_periods(estimates: list, entries: list) -> dict:
    accepted, rejected = credits_from_ledger(list(entries))
    rows = []
    for e in estimates:
        try:
            rows.append(EstimateRow(str(e["market"]), str(e["program_id"]), str(e.get("series") or ""),
                                    Decimal(str(e["estimated_usd"])), str(e.get("period_start") or "")))
        except (KeyError, ArithmeticError, ValueError):
            continue
    matched = reconcile(rows, accepted)
    paid = sum((Decimal(m.paid_usd) for m in matched["matches"]), Decimal(0))
    est = sum((Decimal(m.estimated_usd) for m in matched["matches"]), Decimal(0))
    by_series: dict = {}
    for m in matched["matches"]:
        s = by_series.setdefault(m.series or "?", [Decimal(0), Decimal(0), 0])
        s[0] += Decimal(m.paid_usd)
        s[1] += Decimal(m.estimated_usd)
        s[2] += 1
    reasons: dict = {}
    for r in rejected:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    days = {c.period_start[:10] for c in accepted if c.period_start}
    return {
        "matched": len(matched["matches"]),
        "paid_usd": _fmt(paid), "estimated_usd": _fmt(est),
        "ratio": None if est <= 0 else _fmt(paid / est),
        "by_series": {k: {"paid_usd": _fmt(v[0]), "estimated_usd": _fmt(v[1]), "matched": v[2],
                          "ratio": None if v[1] <= 0 else _fmt(v[0] / v[1])} for k, v in sorted(by_series.items())},
        "unmatched_estimates": len(matched["unmatched_estimates"]),
        "unmatched_estimated_usd": _fmt(sum((r.estimated_usd for r in matched["unmatched_estimates"]), Decimal(0))),
        "unmatched_credits": len(matched["unmatched_credits"]),
        "unmatched_credit_usd": _fmt(sum((c.amount_usd for c in matched["unmatched_credits"]), Decimal(0))),
        "ambiguous": len(matched["ambiguous"]),
        "rejected": reasons, "paid_days": len(days),
    }


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
