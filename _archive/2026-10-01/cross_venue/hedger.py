"""Cross-venue hedger (Track B.2) — log-only initial deploy.

Given a Kalshi fill (new row in fill_ledger), look up the hedge mapping
in `cross_venue.market_match.HEDGE_MAP`, compute the offsetting hedge
size on the external instrument, and record what WOULD have been
executed to `hedge_log`. No external orders placed until B.3 (IBKR)
and B.4 (Kraken) adapters land and `AUTO_HEDGE_ENABLED` flips on.

Why log-only first
------------------
Two weeks of paper data lets us:
  (a) verify the binary→continuous hedge-ratio is producing sane
      sizes (not 0.001 or 50)
  (b) measure the basis residual that would have resulted from
      perfect-execution hedges (i.e. realized Kalshi PnL + ratio-
      sized hedge PnL); fed into `tools/hedge_effectiveness.py`
  (c) validate the strike-parsing on real Kalshi tickers

PIPELINE
--------
1. `tools/fills_sync.py` inserts a new fill row → calls
   `process_fill(fill_id, ticker, side, fill_price_c, fill_size,
                  fill_ts)`.
2. `process_fill` parses the strike from the ticker, fetches the
   latest futures spot from `futures_prices`, computes hedge_qty
   via `HedgeSpec.hedge_qty()`, writes a `hedge_log` row with
   status='would_have_fired' (log-only) or 'placed'/'failed' when
   execution lands.
3. `tools/hedge_effectiveness.py` reads the log + settlement data
   to compute realized basis residual.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings
from cross_venue.market_match import (
    HedgeSpec, hedge_for_ticker, is_explicit_no_hedge, series_prefix,
)

_log = logging.getLogger(__name__)


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS hedge_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_id             TEXT NOT NULL,
    kalshi_ticker       TEXT NOT NULL,
    kalshi_side         TEXT NOT NULL,         -- 'yes' / 'no'
    kalshi_contracts    INTEGER NOT NULL,      -- signed: +long YES, -long NO
    kalshi_fill_price_c INTEGER,
    kalshi_strike       REAL,
    hedge_instrument    TEXT,
    hedge_venue         TEXT,
    spot                REAL,                  -- spot used for delta calc
    sigma_annual        REAL,
    hours_to_settle     REAL,
    hedge_qty_float     REAL,                  -- raw float qty before rounding
    hedge_qty_int       INTEGER,               -- rounded for execution
    hedge_qty_residual  REAL,                  -- fractional residual carried fwd
    status              TEXT NOT NULL,         -- 'would_have_fired' | 'no_hedge' |
                                              -- 'no_spot' | 'parse_failed' | 'placed' | 'failed'
    status_detail       TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (fill_id, kalshi_ticker)
);

CREATE INDEX IF NOT EXISTS idx_hedge_log_ticker     ON hedge_log(kalshi_ticker);
CREATE INDEX IF NOT EXISTS idx_hedge_log_status     ON hedge_log(status);
CREATE INDEX IF NOT EXISTS idx_hedge_log_instrument ON hedge_log(hedge_instrument);
CREATE INDEX IF NOT EXISTS idx_hedge_log_created    ON hedge_log(created_at);
"""


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_DDL)
        conn.commit()
    finally:
        conn.close()


# ── Ticker strike parsing ───────────────────────────────────────────────────
# Kalshi commodity tickers typically encode strike as "-T<value>" at the end:
#   KXBRENTD-26JUN0117-T100
#   KXNATGASD-26MAY-T3.5
#   KXBTCMINMON-26MAY-T100000
#   KXCPIYOY-2604-T2.5
# We capture the numeric trailer. Phase E (2026-05-16): newer crypto LIP
# tickers use a bare "-<strike>" suffix without the "T" prefix
# (e.g. KXXRPMAXMON-XRP-26MAY31-210). Make T optional to cover both.
_STRIKE_RE = re.compile(r"-T?([+-]?\d+(?:\.\d+)?)\s*$")


def parse_strike(ticker: str) -> Optional[float]:
    """Extract the strike from a Kalshi ticker. Returns None if not parseable.

    Handles two formats:
      - Legacy: ...-T<strike>   (KXBRENTD-26JUN0117-T100)
      - Newer:  ...-<strike>    (KXXRPMAXMON-XRP-26MAY31-210)

    Some tickers (sports, politics) don't encode a numeric strike — those
    series should be tagged in NO_HEDGE_SERIES rather than reaching here.
    """
    m = _STRIKE_RE.search(ticker)
    return float(m.group(1)) if m else None


def _latest_spot(db_path: str, prefix: str) -> Optional[float]:
    """Most-recent futures_prices row for a Kalshi series prefix.

    For crypto (Kraken) prefixes we will need a separate spot source;
    until B.4 lands, return None and log status='no_spot'.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            row = conn.execute(
                """SELECT price FROM futures_prices
                   WHERE kalshi_prefix = ?
                   ORDER BY fetched_at DESC LIMIT 1""",
                (prefix,),
            ).fetchone()
        finally:
            conn.close()
        return float(row[0]) if row else None
    except sqlite3.OperationalError:
        return None


def _hours_to_settle_from_ticker(ticker: str) -> Optional[float]:
    """Reuse run_paper._minutes_until_settle parser. We duplicate the
    light logic here to avoid importing the whole runner.

    Format: KXBRENTD-<YYMMMDDHH>-T<strike>. Hour is 17 (5pm ET → 21 UTC)
    for many series; older parsers handled this. For B.2 log-only we
    default to 24h when parsing fails — execution path will be stricter.
    """
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})-T", ticker)
    if not m:
        return None
    yy, mon, dd, hh = m.group(1), m.group(2), m.group(3), m.group(4)
    months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    if mon not in months:
        return None
    try:
        settle = datetime(2000 + int(yy), months[mon], int(dd), int(hh),
                          tzinfo=timezone.utc)
        delta = (settle - datetime.now(timezone.utc)).total_seconds()
        return max(1.0 / 60.0, delta / 3600.0)
    except (ValueError, OverflowError):
        return None


# ── Main entry point ────────────────────────────────────────────────────────

@dataclass
class HedgeDecision:
    fill_id: str
    kalshi_ticker: str
    kalshi_side: str
    kalshi_contracts: int            # signed
    status: str                      # see SCHEMA_DDL.status enum
    status_detail: str = ""
    hedge_spec: Optional[HedgeSpec] = None
    spot: Optional[float] = None
    hours_to_settle: Optional[float] = None
    hedge_qty_float: Optional[float] = None
    hedge_qty_int: Optional[int] = None
    hedge_qty_residual: Optional[float] = None
    kalshi_strike: Optional[float] = None
    kalshi_fill_price_c: Optional[int] = None
    sigma_annual: Optional[float] = None


def decide(fill_id: str, ticker: str, side: str, fill_price_c: Optional[int],
           fill_size: int, fill_ts: float,
           db_path: str = settings.DB_PATH) -> HedgeDecision:
    """Compute hedge intent for a single fill. Does NOT persist.

    Sign of kalshi_contracts: +size = long YES, -size = long NO.
    """
    s = (side or "").lower()
    signed = int(fill_size) if s == "yes" else -int(fill_size)

    if is_explicit_no_hedge(ticker):
        return HedgeDecision(
            fill_id=fill_id, kalshi_ticker=ticker, kalshi_side=s,
            kalshi_contracts=signed, status="no_hedge",
            status_detail="series tagged NO_HEDGE",
            kalshi_fill_price_c=fill_price_c,
        )

    spec = hedge_for_ticker(ticker)
    if spec is None:
        return HedgeDecision(
            fill_id=fill_id, kalshi_ticker=ticker, kalshi_side=s,
            kalshi_contracts=signed, status="no_hedge",
            status_detail=f"no HEDGE_MAP entry for series={series_prefix(ticker)}",
            kalshi_fill_price_c=fill_price_c,
        )

    # Polymarket / same-event hedges bypass strike + spot lookup. The
    # hedge is 1:1 contract count regardless of underlying price.
    if getattr(spec, "hedge_strategy", "delta_binary") == "same_event_unit":
        qty_float = spec.hedge_qty(
            kalshi_contracts=signed, strike_price=0.0,
            spot=0.0, hours_to_settle=0.0,
        )
        qty_int = int(round(qty_float))
        residual = qty_float - qty_int
        return HedgeDecision(
            fill_id=fill_id, kalshi_ticker=ticker, kalshi_side=s,
            kalshi_contracts=signed, status="would_have_fired",
            hedge_spec=spec, spot=None, hours_to_settle=None,
            hedge_qty_float=qty_float, hedge_qty_int=qty_int,
            hedge_qty_residual=residual, kalshi_strike=None,
            kalshi_fill_price_c=fill_price_c,
            sigma_annual=spec.sigma_annual_default,
        )

    strike = parse_strike(ticker)
    if strike is None:
        return HedgeDecision(
            fill_id=fill_id, kalshi_ticker=ticker, kalshi_side=s,
            kalshi_contracts=signed, status="parse_failed",
            status_detail=f"no -T<strike> in ticker {ticker!r}",
            hedge_spec=spec, kalshi_fill_price_c=fill_price_c,
        )

    prefix = series_prefix(ticker)
    spot = _latest_spot(db_path, prefix)
    if spot is None:
        return HedgeDecision(
            fill_id=fill_id, kalshi_ticker=ticker, kalshi_side=s,
            kalshi_contracts=signed, status="no_spot",
            status_detail=(f"no futures_prices row for prefix={prefix} "
                           f"(crypto/perp feeds wired in B.4)"),
            hedge_spec=spec, kalshi_strike=strike,
            kalshi_fill_price_c=fill_price_c,
        )

    hrs = _hours_to_settle_from_ticker(ticker) or 24.0
    qty_float = spec.hedge_qty(
        kalshi_contracts=signed, strike_price=strike,
        spot=spot, hours_to_settle=hrs,
    )
    qty_int = int(round(qty_float))
    residual = qty_float - qty_int
    return HedgeDecision(
        fill_id=fill_id, kalshi_ticker=ticker, kalshi_side=s,
        kalshi_contracts=signed, status="would_have_fired",
        hedge_spec=spec, spot=spot, hours_to_settle=hrs,
        hedge_qty_float=qty_float, hedge_qty_int=qty_int,
        hedge_qty_residual=residual, kalshi_strike=strike,
        kalshi_fill_price_c=fill_price_c, sigma_annual=spec.sigma_annual_default,
    )


def persist(decision: HedgeDecision, db_path: str = settings.DB_PATH) -> bool:
    """Write a HedgeDecision to hedge_log. Returns True on insert."""
    ensure_schema(db_path)
    try:
        conn = sqlite3.connect(db_path, timeout=3.0)
        try:
            conn.execute(
                """INSERT OR IGNORE INTO hedge_log
                   (fill_id, kalshi_ticker, kalshi_side, kalshi_contracts,
                    kalshi_fill_price_c, kalshi_strike,
                    hedge_instrument, hedge_venue, spot, sigma_annual,
                    hours_to_settle,
                    hedge_qty_float, hedge_qty_int, hedge_qty_residual,
                    status, status_detail, created_at)
                   VALUES (?,?,?,?, ?,?, ?,?,?,?, ?, ?,?,?, ?,?,?)""",
                (decision.fill_id, decision.kalshi_ticker, decision.kalshi_side,
                 decision.kalshi_contracts, decision.kalshi_fill_price_c,
                 decision.kalshi_strike,
                 decision.hedge_spec.instrument if decision.hedge_spec else None,
                 decision.hedge_spec.hedge_venue if decision.hedge_spec else None,
                 decision.spot, decision.sigma_annual, decision.hours_to_settle,
                 decision.hedge_qty_float, decision.hedge_qty_int,
                 decision.hedge_qty_residual,
                 decision.status, decision.status_detail or None,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        _log.warning(f"hedger.persist failed for {decision.fill_id}: {e}")
        return False


def _execute_if_enabled(d: HedgeDecision, db_path: str) -> HedgeDecision:
    """When AUTO_HEDGE_ENABLED, call the venue adapter. The adapter itself
    is dry-run unless AUTO_HEDGE_<VENUE> is also True, so the call still
    produces broker_dry_run_log rows but no real orders.

    Updates the HedgeDecision's status:
      'would_have_fired' → 'placed' (or 'failed') when adapter touched
    """
    if not getattr(settings, "AUTO_HEDGE_ENABLED", False):
        return d
    if d.status != "would_have_fired" or d.hedge_spec is None:
        return d
    qty = d.hedge_qty_int or 0
    if qty == 0:
        # Hedge ratio rounded to zero contracts — log and skip.
        d.status_detail = (d.status_detail or "") + " | qty_rounded_to_0"
        return d
    side = "sell" if qty < 0 else "buy"
    abs_qty = abs(qty)

    venue = d.hedge_spec.hedge_venue
    try:
        if venue == "CME":
            from execution.ibkr_adapter import IBKRAdapter
            adapter = IBKRAdapter(db_path=db_path)
            result = adapter.place_market(
                instrument=d.hedge_spec.instrument,
                qty=abs_qty, side=side,
                notes=f"kalshi={d.kalshi_ticker} q={d.kalshi_contracts}",
            )
        elif venue == "Kraken":
            from execution.kraken_adapter import KrakenAdapter
            adapter = KrakenAdapter(db_path=db_path)
            result = adapter.place_market(
                instrument=d.hedge_spec.instrument,
                qty=abs_qty, side=side,
                notes=f"kalshi={d.kalshi_ticker} q={d.kalshi_contracts}",
            )
        elif venue == "Polymarket":
            # Same-event 1:1 hedge. Adapter accepts 'yes'/'no' semantics.
            # qty>0 means we're long YES on Kalshi → buy NO on PM.
            # qty<0 (sell) means we're long NO on Kalshi → buy YES on PM.
            # qty here is the SIGNED hedge_qty_int (already negated vs Kalshi).
            from execution.polymarket_adapter import PolymarketAdapter
            pm_side = "no" if d.kalshi_side == "yes" else "yes"
            adapter = PolymarketAdapter(db_path=db_path)
            # Resolve PM slug from kalshi_pm_map if available; otherwise
            # use the spec.instrument as placeholder. Real-time mapping:
            try:
                from cross_venue.kalshi_pm_map import find_pm_counterpart
                pair = find_pm_counterpart(d.kalshi_ticker)
                pm_slug = pair[1] if pair else d.hedge_spec.instrument
            except Exception:
                pm_slug = d.hedge_spec.instrument
            result = adapter.place_market(
                instrument=pm_slug,
                qty=abs_qty, side=pm_side,
                notes=f"kalshi={d.kalshi_ticker} side={d.kalshi_side} q={d.kalshi_contracts}",
            )
        elif venue == "ICE":
            # ICE has no public retail API; manual hedge or skip.
            d.status_detail = (d.status_detail or "") + " | ICE_manual_only"
            return d
        else:
            d.status_detail = (d.status_detail or "") + f" | unknown_venue:{venue}"
            return d
    except Exception as e:
        d.status = "failed"
        d.status_detail = f"adapter_exception: {e}"
        return d

    if result.status in ("dry_run", "filled", "partial"):
        d.status = "placed"
        d.status_detail = (
            f"adapter_status={result.status} fill_id={result.fill_id} "
            f"avg_price={result.avg_price}"
        )
    else:
        d.status = "failed"
        d.status_detail = f"adapter_status={result.status} detail={result.detail}"
    return d


def process_fill(fill_id: str, ticker: str, side: str,
                 fill_price_c: Optional[int], fill_size: int, fill_ts: float,
                 db_path: str = settings.DB_PATH) -> HedgeDecision:
    """Decide + (optionally execute) + persist for one fill."""
    d = decide(fill_id, ticker, side, fill_price_c, fill_size, fill_ts,
               db_path=db_path)
    d = _execute_if_enabled(d, db_path=db_path)
    persist(d, db_path=db_path)
    return d


# ── Self-test ───────────────────────────────────────────────────────────────

def _self_test() -> None:
    import os, tempfile, time as _time
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # Seed futures_prices table so KXBRENTD has a spot
        conn = sqlite3.connect(path)
        conn.executescript("""
        CREATE TABLE futures_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kalshi_prefix TEXT, yahoo_symbol TEXT,
            name TEXT, price REAL, prev_close REAL, change_pct REAL, volume INTEGER,
            fetched_at TEXT);
        """)
        conn.execute(
            "INSERT INTO futures_prices VALUES (NULL,?,?,?,?,?,?,?,?)",
            ("KXBRENTD", "BZM26.NYM", "Brent", 98.50, 98.00, 0.005, 1000,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()

        # Long YES Kalshi fill on KXBRENTD-26JUN0117-T100 with spot $98.50
        # → expect short BZ position (negative qty), tiny magnitude
        d = process_fill(
            fill_id="TEST-1", ticker="KXBRENTD-26JUN0117-T100",
            side="yes", fill_price_c=40, fill_size=200, fill_ts=_time.time(),
            db_path=path,
        )
        assert d.status == "would_have_fired", d
        assert d.hedge_qty_float is not None and d.hedge_qty_float < 0, d
        # 200 contracts × $1 × delta / 1000 bbl_BZ → small float, likely 0 ints
        assert d.hedge_qty_int in (-1, 0), d

        # No-spot case (e.g. crypto without feed yet)
        d2 = process_fill(
            fill_id="TEST-2", ticker="KXBTCMINMON-26MAY-T100000",
            side="yes", fill_price_c=50, fill_size=10, fill_ts=_time.time(),
            db_path=path,
        )
        assert d2.status == "no_spot", d2

        # No-hedge series
        d3 = process_fill(
            fill_id="TEST-3", ticker="KXPRES-2028-foo",
            side="yes", fill_price_c=60, fill_size=5, fill_ts=_time.time(),
            db_path=path,
        )
        assert d3.status == "no_hedge", d3

        # Unmapped commodity
        d4 = process_fill(
            fill_id="TEST-4", ticker="KXFOO-26MAY-T100",
            side="no", fill_price_c=45, fill_size=8, fill_ts=_time.time(),
            db_path=path,
        )
        assert d4.status == "no_hedge", d4

        # Verify rows landed
        conn = sqlite3.connect(path)
        n = conn.execute("SELECT COUNT(*) FROM hedge_log").fetchone()[0]
        conn.close()
        assert n == 4, f"expected 4 rows, got {n}"

        # Idempotent (UNIQUE on (fill_id, kalshi_ticker))
        d5 = process_fill(
            fill_id="TEST-1", ticker="KXBRENTD-26JUN0117-T100",
            side="yes", fill_price_c=40, fill_size=200, fill_ts=_time.time(),
            db_path=path,
        )
        conn = sqlite3.connect(path)
        n2 = conn.execute("SELECT COUNT(*) FROM hedge_log").fetchone()[0]
        conn.close()
        assert n2 == 4, f"idempotency failed: {n2} != 4"

        print(f"hedger self-test: PASS "
              f"(BZ would-have qty={d.hedge_qty_float:+.4f}, "
              f"int={d.hedge_qty_int}, residual={d.hedge_qty_residual:+.4f})")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    _self_test()
