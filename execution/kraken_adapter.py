"""Kraken Pro adapter for crypto-perp hedging (Track B.4).

Mirror of execution.ibkr_adapter — same FillResult shape, same
methods (`heartbeat`, `place_market`, `cancel`, `get_position`). The
cross-venue hedger routes Kraken-venue HedgeSpecs here when
AUTO_HEDGE_ENABLED + AUTO_HEDGE_KRAKEN are both True.

DRY-RUN DEFAULT
---------------
Default is dry_run unless AUTO_HEDGE_KRAKEN=True. In dry_run, orders
are logged to `broker_dry_run_log` (same table as IBKR) and
`broker_health` heartbeats land. No real Kraken API calls are made.

ENVIRONMENT (when going live)
-----------------------------
- KRAKEN_API_KEY
- KRAKEN_API_SECRET
- KRAKEN_API_URL  (default https://api.kraken.com)

DEPENDENCIES (only when AUTO_HEDGE_KRAKEN=True)
-----------------------------------------------
    pip install krakenex     # or ccxt for a unified-exchange shim

When the dep is missing, the adapter falls back to dry_run with a
single WARN.

INSTRUMENT NOTES
----------------
Kraken futures use venue-specific symbols (PI_XBTUSD, PI_ETHUSD, etc.)
while the cross_venue.market_match.HEDGE_MAP lists the spot-style
symbols (XBTUSD, ETHUSD). This adapter does NOT translate — it passes
the symbol from HedgeSpec.instrument verbatim, on the assumption the
operator updates HEDGE_MAP with the venue-correct symbol once a live
account is open.

USAGE
-----
    from execution.kraken_adapter import KrakenAdapter
    a = KrakenAdapter()                 # dry_run by default
    a.heartbeat()
    fill = a.place_market("XBTUSD", qty=1, side="sell")
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings
from execution.ibkr_adapter import FillResult, ensure_schema as _ensure_health_schema

_log = logging.getLogger(__name__)


class KrakenAdapter:
    """Same interface as IBKRAdapter; different venue tag.

    All schema tables are shared with the IBKR adapter — broker_health
    and broker_dry_run_log — keyed on `venue='kraken'`.
    """

    venue_name = "kraken"

    def __init__(self,
                 dry_run: Optional[bool] = None,
                 api_url: Optional[str] = None,
                 db_path: Optional[str] = None) -> None:
        self.dry_run = (
            dry_run if dry_run is not None
            else not getattr(settings, "AUTO_HEDGE_KRAKEN", False)
        )
        self.api_url = api_url or os.getenv("KRAKEN_API_URL",
                                            "https://api.kraken.com")
        self.api_key = os.getenv("KRAKEN_API_KEY", "")
        self.api_secret = os.getenv("KRAKEN_API_SECRET", "")
        self.db_path = db_path or settings.DB_PATH
        self._kx = None    # krakenex.API() handle, lazy
        _ensure_health_schema(self.db_path)   # shared broker_* tables

    def _connect_if_live(self) -> bool:
        if self.dry_run:
            return False
        if self._kx is not None:
            return True
        try:
            import krakenex  # type: ignore
        except ImportError:
            _log.warning(
                "krakenex not installed; staying dry-run. "
                "`pip install krakenex` to enable AUTO_HEDGE_KRAKEN."
            )
            self.dry_run = True
            return False
        if not (self.api_key and self.api_secret):
            _log.warning("KRAKEN_API_KEY/SECRET not set; staying dry-run.")
            self.dry_run = True
            return False
        try:
            self._kx = krakenex.API(key=self.api_key, secret=self.api_secret)
            return True
        except Exception as e:
            _log.warning(f"Kraken init failed: {e} — staying dry-run")
            return False

    def heartbeat(self) -> bool:
        if self.dry_run:
            status = "dry_run"
            detail = "AUTO_HEDGE_KRAKEN=False"
        elif self._connect_if_live():
            status = "up"
            detail = self.api_url
        else:
            status = "down"
            detail = "init failed"
        now = time.time()
        try:
            conn = sqlite3.connect(self.db_path, timeout=1.0)
            try:
                conn.execute(
                    "INSERT INTO broker_health(venue, status, detail, ts, ts_iso) "
                    "VALUES (?,?,?,?,?)",
                    (self.venue_name, status, detail, now,
                     datetime.fromtimestamp(now, tz=timezone.utc).isoformat()),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            _log.debug(f"broker_health write failed: {e}")
        return status in ("up", "dry_run")

    def place_market(self, instrument: str, qty: int, side: str,
                     *, notes: str = "") -> FillResult:
        side = side.lower()
        if side not in ("buy", "sell"):
            return FillResult(fill_id="", instrument=instrument, side=side,
                              qty=qty, status="rejected",
                              detail=f"invalid side {side!r}")
        if qty <= 0:
            return FillResult(fill_id="", instrument=instrument, side=side,
                              qty=qty, status="rejected",
                              detail=f"qty<=0 ({qty})")

        if self.dry_run:
            fid = f"DRY-{uuid.uuid4().hex[:16]}"
            now_iso = datetime.now(timezone.utc).isoformat()
            try:
                conn = sqlite3.connect(self.db_path, timeout=2.0)
                try:
                    conn.execute(
                        """INSERT INTO broker_dry_run_log
                           (venue, instrument, qty, side, intended_at,
                            synthetic_fill_id, notes)
                           VALUES (?,?,?,?,?,?,?)""",
                        (self.venue_name, instrument, qty, side, now_iso,
                         fid, notes or None),
                    )
                    conn.commit()
                finally:
                    conn.close()
            except Exception as e:
                _log.debug(f"dry_run log failed: {e}")
            _log.info(f"[Kraken dry_run] {side.upper()} {qty} {instrument}  fid={fid}")
            return FillResult(fill_id=fid, instrument=instrument, side=side,
                              qty=qty, status="dry_run", detail=notes)

        if not self._connect_if_live():
            return FillResult(fill_id="", instrument=instrument, side=side,
                              qty=qty, status="rejected", detail="not_connected")
        try:
            resp = self._kx.query_private("AddOrder", {
                "pair": instrument,
                "type": side,            # 'buy'/'sell'
                "ordertype": "market",
                "volume": str(qty),
            })
            if resp.get("error"):
                return FillResult(fill_id="", instrument=instrument, side=side,
                                  qty=qty, status="rejected",
                                  detail=";".join(resp["error"]))
            txid = (resp.get("result") or {}).get("txid", [""])[0]
            return FillResult(fill_id=txid, instrument=instrument, side=side,
                              qty=qty, status="filled")
        except Exception as e:
            _log.error(f"Kraken AddOrder failed: {e}")
            return FillResult(fill_id="", instrument=instrument, side=side,
                              qty=qty, status="rejected", detail=str(e))

    def cancel(self, order_id: str) -> bool:
        if self.dry_run:
            _log.info(f"[Kraken dry_run] CANCEL {order_id}")
            return True
        if not self._connect_if_live():
            return False
        try:
            resp = self._kx.query_private("CancelOrder", {"txid": order_id})
            return not resp.get("error")
        except Exception as e:
            _log.warning(f"Kraken cancel failed: {e}")
            return False

    def get_position(self, instrument: str) -> int:
        # Dry-run: same logic as IBKR — sum signed dry_run_log entries.
        if self.dry_run:
            try:
                conn = sqlite3.connect(self.db_path, timeout=2.0)
                try:
                    rows = conn.execute(
                        """SELECT side, SUM(qty) FROM broker_dry_run_log
                           WHERE venue=? AND instrument=?
                           GROUP BY side""",
                        (self.venue_name, instrument),
                    ).fetchall()
                finally:
                    conn.close()
            except sqlite3.OperationalError:
                return 0
            net = 0
            for side, q in rows:
                net += int(q or 0) if side == "buy" else -int(q or 0)
            return net
        if not self._connect_if_live():
            return 0
        # Live: Kraken's /OpenPositions endpoint
        try:
            resp = self._kx.query_private("OpenPositions", {})
            for _, p in (resp.get("result") or {}).items():
                if p.get("pair") == instrument:
                    vol = float(p.get("vol", 0))
                    return int(vol) if p.get("type") == "buy" else -int(vol)
            return 0
        except Exception as e:
            _log.warning(f"Kraken get_position failed: {e}")
            return 0


def _self_test() -> None:
    import os as _os, tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    _os.close(fd)
    try:
        a = KrakenAdapter(dry_run=True, db_path=path)
        assert a.heartbeat() is True
        f = a.place_market("XBTUSD", 1, "sell")
        assert f.status == "dry_run" and f.fill_id.startswith("DRY-")
        f2 = a.place_market("XBTUSD", 2, "buy")
        assert f2.status == "dry_run"
        pos = a.get_position("XBTUSD")
        assert pos == 1, pos      # 2 buy − 1 sell = +1
        assert a.cancel(f.fill_id) is True
        assert a.place_market("XBTUSD", 0, "buy").status == "rejected"
        print("kraken_adapter self-test: PASS")
    finally:
        try:
            _os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    _self_test()
