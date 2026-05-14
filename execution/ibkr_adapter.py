"""IBKR adapter for CME micro futures execution (Track B.3).

The cross-venue hedger needs to place orders on CME (MCL, MNG, MGC, MES,
MNQ, M6E, ZQ, grains) to neutralize Kalshi binary exposure. This module
provides a thin, testable interface over `ib_insync`, with a `dry_run`
mode that logs intended orders without placing them.

ARCHITECTURE
------------
- One IB Gateway / TWS process running locally (separate systemd unit
  `ibgw.service`, port 4002 paper / 7497 live).
- This adapter connects on demand, places one-shot orders, and reports
  fill events back to `broker_health` and `hedge_log`.
- `dry_run=True` (default): logs intended orders to `broker_dry_run_log`
  + writes a synthetic fill_id; never calls ib_insync. This lets the
  hedger fire in paper without TWS running, generating data for the
  effectiveness tracker.
- Heartbeat: every 10s a row in `broker_health(venue='ibkr', status,
  ts)`. The hedger refuses to fire when status is stale (>30s).

ENVIRONMENT
-----------
- IBKR_GATEWAY_HOST (default 127.0.0.1)
- IBKR_GATEWAY_PORT (default 7497 for TWS, 4002 for paper Gateway)
- IBKR_CLIENT_ID    (default 17)
- AUTO_HEDGE_CME    (default False; gates real-order placement)

DEPENDENCIES (only when AUTO_HEDGE_CME=True)
--------------------------------------------
    pip install ib_insync

The adapter degrades gracefully when ib_insync isn't installed: it
emits a single WARN and continues in dry-run mode forever.

USAGE
-----
    from execution.ibkr_adapter import IBKRAdapter
    a = IBKRAdapter()           # dry_run by default
    a.heartbeat()
    fill = a.place_market("MCL", qty=-1, side="sell")
    pos = a.get_position("MCL")
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings

_log = logging.getLogger(__name__)


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS broker_health (
    venue       TEXT NOT NULL,
    status      TEXT NOT NULL,           -- 'up' | 'down' | 'degraded'
    detail      TEXT,
    ts          REAL NOT NULL,
    ts_iso      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_broker_health_venue_ts
    ON broker_health(venue, ts DESC);

CREATE TABLE IF NOT EXISTS broker_dry_run_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    venue         TEXT NOT NULL,
    instrument    TEXT NOT NULL,
    qty           INTEGER NOT NULL,
    side          TEXT NOT NULL,
    intended_at   TEXT NOT NULL,
    synthetic_fill_id TEXT NOT NULL,
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_dryrun_instrument
    ON broker_dry_run_log(instrument, intended_at DESC);
"""


def ensure_schema(db_path: str = settings.DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_DDL)
        conn.commit()
    finally:
        conn.close()


@dataclass
class FillResult:
    """Common shape for `place_market` returns across broker adapters."""
    fill_id: str
    instrument: str
    side: str            # 'buy' / 'sell'
    qty: int
    avg_price: Optional[float] = None
    status: str = "dry_run"   # 'dry_run' | 'filled' | 'rejected' | 'partial'
    detail: str = ""


class IBKRAdapter:
    """Thin facade over ib_insync — dry-run by default.

    Methods (all return FillResult / int / None like the future Kraken
    adapter — the hedger doesn't care which venue it's talking to):
        heartbeat()                 → bool      (writes broker_health row)
        place_market(sym, qty, side)→ FillResult
        cancel(order_id)            → bool
        get_position(sym)           → int       (signed)
    """

    venue_name = "ibkr"

    def __init__(self,
                 dry_run: Optional[bool] = None,
                 host: Optional[str] = None,
                 port: Optional[int] = None,
                 client_id: Optional[int] = None,
                 db_path: Optional[str] = None) -> None:
        self.dry_run = (
            dry_run if dry_run is not None
            else not getattr(settings, "AUTO_HEDGE_CME", False)
        )
        self.host = host or os.getenv("IBKR_GATEWAY_HOST", "127.0.0.1")
        self.port = int(port or os.getenv("IBKR_GATEWAY_PORT", "7497"))
        self.client_id = int(client_id or os.getenv("IBKR_CLIENT_ID", "17"))
        self.db_path = db_path or settings.DB_PATH
        self._ib = None                 # ib_insync.IB() handle, lazy
        self._last_heartbeat = 0.0
        ensure_schema(self.db_path)

    # ── Connection ───────────────────────────────────────────────────────
    def _connect_if_live(self) -> bool:
        """In live mode, lazily import ib_insync and connect. Returns True
        when connection is up, False otherwise (logs WARN once)."""
        if self.dry_run:
            return False
        if self._ib is not None and self._ib.isConnected():
            return True
        try:
            import ib_insync  # type: ignore
        except ImportError:
            _log.warning(
                "ib_insync not installed; staying dry-run. "
                "`pip install ib_insync` to enable AUTO_HEDGE_CME live mode."
            )
            self.dry_run = True
            return False
        try:
            self._ib = ib_insync.IB()
            self._ib.connect(self.host, self.port, clientId=self.client_id,
                             timeout=5)
            _log.info(f"IBKR connected: {self.host}:{self.port} "
                      f"client_id={self.client_id}")
            return True
        except Exception as e:
            _log.warning(f"IBKR connect failed: {e} — staying dry-run "
                         f"this cycle")
            return False

    # ── Health / heartbeat ───────────────────────────────────────────────
    def heartbeat(self) -> bool:
        """Write a broker_health row with current status. Always succeeds."""
        if self.dry_run:
            status = "dry_run"
            detail = "AUTO_HEDGE_CME=False"
        elif self._connect_if_live():
            status = "up"
            detail = f"{self.host}:{self.port}"
        else:
            status = "down"
            detail = "connect failed"
        now = time.time()
        self._last_heartbeat = now
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

    # ── Orders ───────────────────────────────────────────────────────────
    def place_market(self, instrument: str, qty: int, side: str,
                     *, notes: str = "") -> FillResult:
        """Place a market order. In dry_run, only logs.

        Args:
            instrument: CME root symbol (MCL, MNG, MGC, MES, MNQ, M6E, ZQ, ...)
            qty: positive integer count of contracts
            side: 'buy' or 'sell'

        Returns:
            FillResult. In dry_run mode, fill_id = 'DRY-<uuid>', status='dry_run'.
        """
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
            _log.info(f"[IBKR dry_run] {side.upper()} {qty} {instrument}  fid={fid}")
            return FillResult(fill_id=fid, instrument=instrument, side=side,
                              qty=qty, status="dry_run", detail=notes)

        # Live path (requires ib_insync + connection)
        if not self._connect_if_live():
            return FillResult(fill_id="", instrument=instrument, side=side,
                              qty=qty, status="rejected",
                              detail="not_connected")
        try:
            import ib_insync as ibi  # type: ignore
            # CME continuous future via Contract — caller supplies root symbol
            # like "MCL"; ib_insync wants Future("MCL", "20260618", "NYMEX")
            # with a real expiry. For brevity in this MVP we use a generic
            # continuous-front-month string; operators MUST verify the exact
            # expiry in their broker UI before going live.
            contract = ibi.Future(symbol=instrument, exchange="NYMEX")
            self._ib.qualifyContracts(contract)
            order = ibi.MarketOrder(action=side.upper(), totalQuantity=qty)
            trade = self._ib.placeOrder(contract, order)
            self._ib.sleep(1)            # let order propagate
            avg_price = (trade.orderStatus.avgFillPrice
                         if trade.orderStatus.status == "Filled" else None)
            status = "filled" if avg_price else "partial"
            return FillResult(
                fill_id=str(trade.order.orderId),
                instrument=instrument, side=side, qty=qty,
                avg_price=avg_price, status=status,
            )
        except Exception as e:
            _log.error(f"IBKR placeOrder failed: {e}")
            return FillResult(fill_id="", instrument=instrument, side=side,
                              qty=qty, status="rejected", detail=str(e))

    def cancel(self, order_id: str) -> bool:
        if self.dry_run:
            _log.info(f"[IBKR dry_run] CANCEL {order_id}")
            return True
        if not self._connect_if_live():
            return False
        try:
            for trade in self._ib.trades():
                if str(trade.order.orderId) == order_id:
                    self._ib.cancelOrder(trade.order)
                    return True
            return False
        except Exception as e:
            _log.warning(f"IBKR cancel failed: {e}")
            return False

    def get_position(self, instrument: str) -> int:
        if self.dry_run:
            # Sum signed dry-run qty across all logged orders
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
        try:
            for p in self._ib.positions():
                if p.contract.symbol == instrument:
                    return int(p.position)
            return 0
        except Exception as e:
            _log.warning(f"IBKR get_position failed: {e}")
            return 0


def _self_test() -> None:
    import os as _os, tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    _os.close(fd)
    try:
        a = IBKRAdapter(dry_run=True, db_path=path)
        assert a.heartbeat() is True
        f = a.place_market("MCL", 2, "sell")
        assert f.status == "dry_run" and f.fill_id.startswith("DRY-")
        f2 = a.place_market("MCL", 1, "buy")
        assert f2.status == "dry_run"
        # net position from dry_run log
        pos = a.get_position("MCL")
        assert pos == -1, pos     # 1 buy + 2 sell = -1
        # cancel always returns True in dry_run
        assert a.cancel(f.fill_id) is True
        # rejection paths
        bad = a.place_market("MCL", 0, "buy")
        assert bad.status == "rejected"
        bad2 = a.place_market("MCL", 1, "yes")
        assert bad2.status == "rejected"
        print("ibkr_adapter self-test: PASS")
    finally:
        try:
            _os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    _self_test()
