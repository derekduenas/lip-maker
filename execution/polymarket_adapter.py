"""Polymarket US adapter — cross-venue same-event hedger.

Mirror of execution.kraken_adapter — same FillResult shape, same methods.
Cross-venue hedger routes Polymarket-venue HedgeSpecs here when
AUTO_HEDGE_ENABLED + AUTO_HEDGE_POLYMARKET are both True.

DRY-RUN DEFAULT
---------------
Default is dry_run unless AUTO_HEDGE_POLYMARKET=True. In dry_run, orders
log to `broker_dry_run_log` (venue='polymarket') and `broker_health`
heartbeats land. No real PM CLOB v2 API calls are made.

WHY POLYMARKET IS UNIQUE
------------------------
Kraken/IBKR hedge by buying/selling the underlying. Polymarket hedges
by taking the OPPOSITE SIDE of the same event:

  Kalshi: long YES 100 contracts of "Will BTC > $100K by Dec 31"
  Hedge:  buy NO 100 contracts of equivalent PM market
  Net:    long 100 of (YES + NO) = guaranteed $1 payoff regardless of outcome

So hedge_qty is 1:1 in contract count, not delta-based. The hedger
must use spec.hedge_strategy="same_event_unit" to route this correctly.

ENVIRONMENT (when going live)
-----------------------------
- PM_API_KEY                (CLOB v2 API key from polymarket.com/markets)
- PM_SECRET                 (HMAC secret for signing orders)
- PM_USDC_WALLET_ADDRESS    (the deposit address Polymarket US assigned)

Live trading also requires:
- KYC complete on polymarket.com
- USDC deposited via Polymarket US wallet (separate from offshore PM)
- CLOB v2 endpoint enabled (Feb 2026 launch onward)

INSTRUMENT NOTES
----------------
PM identifies markets by `slug` (URL-friendly string) or `condition_id`
(deterministic hash). The hedger looks up the slug via
`cross_venue.kalshi_pm_map.find_pm_counterpart(kalshi_ticker)`.
HedgeSpec.instrument carries the slug at decide-time.

PM markets have TWO outcome tokens (YES / NO). Each is a separate
ERC-1155 token with its own token_id. The CLOB v2 API accepts orders
by side ("yes"/"no") which it then translates to the right token_id.
Our adapter uses "yes"/"no" semantics throughout.
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


class PolymarketAdapter:
    """Same interface as IBKRAdapter / KrakenAdapter; different venue tag.

    Shares broker_health + broker_dry_run_log with the other adapters,
    keyed on venue='polymarket'.
    """

    venue_name = "polymarket"

    def __init__(self,
                 dry_run: Optional[bool] = None,
                 api_url: Optional[str] = None,
                 db_path: Optional[str] = None) -> None:
        self.dry_run = (
            dry_run if dry_run is not None
            else not getattr(settings, "AUTO_HEDGE_POLYMARKET", False)
        )
        self.api_url = api_url or os.getenv(
            "PM_API_URL", "https://clob.polymarket.com",
        )
        self.api_key = os.getenv("PM_API_KEY", "")
        self.secret = os.getenv("PM_SECRET", "")
        self.wallet = os.getenv("PM_USDC_WALLET_ADDRESS", "")
        self.db_path = db_path or settings.DB_PATH
        self._client = None    # lazy init
        _ensure_health_schema(self.db_path)

    def _connect_if_live(self) -> bool:
        if self.dry_run:
            return False
        if self._client is not None:
            return True
        # Attempt to import the SDK installed at /root/polymarket-maker/venv
        try:
            # Try the upstream SDK if available
            from polymarket_us.client import PolymarketUSClient  # type: ignore
        except ImportError:
            _log.warning(
                "polymarket_us SDK not installed in lip-maker venv; "
                "staying dry-run. Install via: "
                "/root/lip-maker/venv/bin/pip install polymarket-us"
            )
            self.dry_run = True
            return False
        if not (self.api_key and self.secret):
            _log.warning("PM_API_KEY/SECRET not set; staying dry-run.")
            self.dry_run = True
            return False
        try:
            self._client = PolymarketUSClient(
                api_url=self.api_url,
                api_key=self.api_key,
                secret=self.secret,
            )
            return True
        except Exception as e:
            _log.warning(f"Polymarket init failed: {e} — staying dry-run")
            return False

    def heartbeat(self) -> bool:
        if self.dry_run:
            status = "dry_run"
            detail = "AUTO_HEDGE_POLYMARKET=False"
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
        """instrument = PM slug or condition_id, side = 'yes'/'no'."""
        side = side.lower()
        if side not in ("yes", "no", "buy", "sell"):
            return FillResult(fill_id="", instrument=instrument, side=side,
                              qty=qty, status="rejected",
                              detail=f"invalid side {side!r}")
        # Normalize buy/sell to yes/no for PM API (PM trades in outcome tokens,
        # not buy/sell of an underlying — the hedger passes 'sell' when closing
        # a long, which on PM means BUY the OPPOSITE outcome)
        # Convention used by cross_venue.hedger: 'sell' = close-long
        # PM caller is expected to pass yes/no directly.
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
            _log.info(
                f"[Polymarket dry_run] {side.upper()} {qty} contracts of "
                f"{instrument}  fid={fid}"
            )
            return FillResult(fill_id=fid, instrument=instrument, side=side,
                              qty=qty, status="dry_run", detail=notes)

        # Live path (placeholder — wired when account credentials arrive)
        if not self._connect_if_live():
            return FillResult(fill_id="", instrument=instrument, side=side,
                              qty=qty, status="rejected", detail="not_connected")
        try:
            # PolymarketUS SDK call shape (varies by SDK version):
            # resp = self._client.create_order(
            #     slug=instrument, side=side, qty=qty, order_type="market"
            # )
            # txid = resp.get("order_id", "")
            # For now, stub — live path needs SDK confirmed
            return FillResult(fill_id="STUB", instrument=instrument, side=side,
                              qty=qty, status="rejected",
                              detail="live PM path not yet wired; "
                                     "supply SDK call signature first")
        except Exception as e:
            _log.error(f"Polymarket place_market failed: {e}")
            return FillResult(fill_id="", instrument=instrument, side=side,
                              qty=qty, status="rejected", detail=str(e))

    def cancel(self, order_id: str) -> bool:
        if self.dry_run:
            _log.info(f"[Polymarket dry_run] CANCEL {order_id}")
            return True
        if not self._connect_if_live():
            return False
        # Live path stub — wire when credentials arrive
        _log.warning("PM cancel live path not yet wired")
        return False

    def get_position(self, instrument: str) -> int:
        """Net contracts for the given slug from broker_dry_run_log.

        Note: PM positions are 2-sided (YES + NO tokens). For B.6 unwind
        the hedger always closes by taking the OPPOSITE side of the
        original open, so net position math is: long_yes - long_no.
        """
        if self.dry_run:
            try:
                conn = sqlite3.connect(self.db_path, timeout=2.0)
                try:
                    rows = conn.execute(
                        """SELECT side, SUM(qty) FROM broker_dry_run_log
                           WHERE venue=? AND instrument=?
                             AND datetime(intended_at) >= datetime('now', '-1 day')
                           GROUP BY side""",
                        (self.venue_name, instrument),
                    ).fetchall()
                finally:
                    conn.close()
            except sqlite3.OperationalError:
                return 0
            net = 0
            for side, q in rows:
                net += int(q or 0) if side == "yes" else -int(q or 0)
            return net
        # Live path stub
        return 0


def _self_test() -> None:
    import os as _os, tempfile
    fd, path = _os.path.join("/tmp", f"pm-adapter-test-{uuid.uuid4().hex[:8]}.db"), None
    fd = "/tmp/pm-adapter-test.db"
    if _os.path.exists(fd):
        _os.unlink(fd)
    try:
        a = PolymarketAdapter(dry_run=True, db_path=fd)
        assert a.heartbeat() is True
        f = a.place_market("will-btc-reach-100k-2026", 100, "no")
        assert f.status == "dry_run" and f.fill_id.startswith("DRY-"), f
        f2 = a.place_market("will-btc-reach-100k-2026", 50, "yes")
        assert f2.status == "dry_run", f2
        # Net = 50 yes - 100 no = -50
        pos = a.get_position("will-btc-reach-100k-2026")
        assert pos == -50, pos
        assert a.cancel(f.fill_id) is True
        # Rejected on qty=0
        assert a.place_market("will-btc-reach-100k-2026", 0, "yes").status == "rejected"
        # Rejected on invalid side
        assert a.place_market("will-btc-reach-100k-2026", 1, "bogus").status == "rejected"
        print("polymarket_adapter self-test: PASS")
    finally:
        if _os.path.exists(fd):
            _os.unlink(fd)


if __name__ == "__main__":
    _self_test()
