"""Live Kalshi order executor with RSA-PSS auth.

Takes a list of TermThesis + bankroll, iterates each qualifying thesis,
and places a LIMIT order at the specified side/price/size. Gated by
risk_gates.check_gates — ALL gates must pass per order.

Order API (Kalshi POST /portfolio/orders):
  {
    "ticker": "KXFEDMENTION-26APR-TARI",
    "side": "yes" | "no",
    "action": "buy",
    "type": "limit",
    "count": contracts,
    "yes_price": cents   (for side=yes)
    "no_price": cents    (for side=no)
    "client_order_id": unique_id
  }

Paper mode (SOV_LIVE unset or false): all calls return fake order IDs but
log to DB as if live. Flip SOV_LIVE=true to hit production.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prep.risk_gates import OrderCandidate, check_gates, load_daily_pnl

_log = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


@dataclass
class PlacedOrder:
    """Record of an order we submitted (paper or live)."""
    ticker:        str
    side:          str
    price_cents:   int
    contracts:     int
    order_id:      str
    status:        str          # "accepted" | "rejected" | "paper"
    reason:        str = ""
    event_id:      str = ""
    placed_at:     datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    paper:         bool = True


def _load_env_file(path: Path):
    """Simple .env loader (we avoid python-dotenv dependency)."""
    try:
        for line in path.read_text().splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip("'").strip('"')
            if k and k not in os.environ:
                os.environ[k] = v
    except FileNotFoundError:
        pass


class KalshiOrderClient:
    """RSA-PSS signed client for POST /portfolio/orders."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        private_key_path: Optional[str] = None,
        base_url: str = KALSHI_BASE,
    ):
        # Try Sovereign .env first, then lip-maker's
        _load_env_file(Path(__file__).resolve().parent.parent / ".env")
        _load_env_file(Path("/root/lip-maker/.env"))

        self.api_key = api_key or os.environ.get("KALSHI_KEY_ID") \
                       or os.environ.get("KALSHI_API_KEY", "")
        self.private_key_path = (
            private_key_path
            or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
            or "/root/lip-maker/config/kalshi_private_key.pem"
        )
        self.base_url = base_url.rstrip("/")
        self._private_key = None

    def _load_key(self):
        if self._private_key is not None:
            return
        from cryptography.hazmat.primitives import serialization
        with open(self.private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _sign(self, method: str, path: str) -> dict:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        self._load_key()
        sign_path = path.split("?", 1)[0]
        if not sign_path.startswith("/trade-api/v2"):
            sign_path = "/trade-api/v2" + sign_path
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method.upper()}{sign_path}".encode()
        sig = self._private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY":       self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "Content-Type":            "application/json",
        }

    def get_balance_usd(self) -> float:
        """Fetch current Kalshi balance (cents → dollars)."""
        path = "/portfolio/balance"
        headers = self._sign("GET", path)
        r = requests.get(self.base_url + path, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        return float(data.get("balance", 0) or 0) / 100.0

    def post_order(self, body: dict) -> dict:
        path = "/portfolio/orders"
        headers = self._sign("POST", path)
        r = requests.post(self.base_url + path, headers=headers,
                          data=json.dumps(body), timeout=10)
        if r.status_code >= 400:
            try:
                err = r.json()
            except Exception:
                err = r.text
            raise RuntimeError(f"Kalshi {r.status_code}: {err}")
        return r.json()


def _schema(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sovereign_orders (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id         TEXT NOT NULL,
            ticker           TEXT NOT NULL,
            side             TEXT NOT NULL,
            price_cents      INTEGER NOT NULL,
            contracts        INTEGER NOT NULL,
            cost_usd         REAL NOT NULL,
            order_id         TEXT,
            status           TEXT NOT NULL,
            reason           TEXT,
            paper            INTEGER NOT NULL,
            placed_at        TEXT NOT NULL,
            thesis_payload   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sov_orders_event ON sovereign_orders(event_id);
        CREATE INDEX IF NOT EXISTS idx_sov_orders_time  ON sovereign_orders(placed_at);
        """)
        conn.commit()
    finally:
        conn.close()


def _log_order(db_path: str, po: PlacedOrder, cost_usd: float, thesis_payload: dict):
    _schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO sovereign_orders
               (event_id, ticker, side, price_cents, contracts, cost_usd,
                order_id, status, reason, paper, placed_at, thesis_payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (po.event_id, po.ticker, po.side, po.price_cents, po.contracts,
             cost_usd, po.order_id, po.status, po.reason,
             1 if po.paper else 0, po.placed_at.isoformat(),
             json.dumps(thesis_payload)),
        )
        conn.commit()
    finally:
        conn.close()


def place_order(
    candidate: OrderCandidate,
    client:    Optional[KalshiOrderClient] = None,
    db_path:   str = "/root/sovereign/data/sovereign.db",
    thesis_payload: Optional[dict] = None,
    dry_run:   bool = False,
) -> PlacedOrder:
    """Place a single order. ALL risk gates enforced.

    Paper mode is the DEFAULT — returns fake order_id. Set SOV_LIVE=true env
    to hit production. Pass dry_run=True to bypass env check (tests only).

    Returns PlacedOrder with status in {"accepted", "rejected", "paper", "error"}.
    """
    client = client or KalshiOrderClient()
    thesis_payload = thesis_payload or {}

    # Balance + P&L state (needed by gates)
    is_live = os.environ.get("SOV_LIVE", "").lower() in ("1", "true", "yes")
    if is_live and not dry_run:
        try:
            balance = client.get_balance_usd()
        except Exception as e:
            _log.error(f"balance fetch failed: {e}")
            po = PlacedOrder(
                ticker=candidate.ticker, side=candidate.side,
                price_cents=candidate.price_cents, contracts=candidate.contracts,
                order_id="", status="error", reason=f"balance_fetch_failed: {e}",
                event_id=candidate.event_id, paper=True,
            )
            _log_order(db_path, po, candidate.cost_usd(), thesis_payload)
            return po
    else:
        balance = candidate.bankroll_usd  # use bankroll as proxy in paper

    daily_pnl, worst_single = load_daily_pnl(db_path)

    # GATE CHECK
    # In paper/dry-run mode, G2 (SOV_LIVE requirement) is tautological — skip it.
    # All other gates (balance floor, position caps, daily-loss) still validate.
    ok, reason = check_gates(candidate, balance, daily_pnl, worst_single,
                              skip_live_check=(dry_run or not is_live))
    if not ok:
        po = PlacedOrder(
            ticker=candidate.ticker, side=candidate.side,
            price_cents=candidate.price_cents, contracts=candidate.contracts,
            order_id="", status="rejected", reason=reason,
            event_id=candidate.event_id, paper=True,
        )
        _log_order(db_path, po, candidate.cost_usd(), thesis_payload)
        return po

    # PLACE (live or paper)
    coid = f"SOV-{uuid.uuid4().hex[:16]}"
    if not is_live or dry_run:
        po = PlacedOrder(
            ticker=candidate.ticker, side=candidate.side,
            price_cents=candidate.price_cents, contracts=candidate.contracts,
            order_id=f"PAPER-{coid}", status="paper", reason="paper_mode",
            event_id=candidate.event_id, paper=True,
        )
        _log_order(db_path, po, candidate.cost_usd(), thesis_payload)
        _log.info(f"[PAPER] PLACE {po.ticker} {po.side}@{po.price_cents}c x{po.contracts}")
        return po

    # LIVE
    body = {
        "ticker": candidate.ticker,
        "side":   candidate.side,
        "action": "buy",
        "type":   "limit",
        "count":  candidate.contracts,
        "client_order_id": coid,
    }
    if candidate.side == "yes":
        body["yes_price"] = candidate.price_cents
    else:
        body["no_price"] = candidate.price_cents

    try:
        resp = client.post_order(body)
        order_id = resp.get("order", {}).get("order_id") or coid
        po = PlacedOrder(
            ticker=candidate.ticker, side=candidate.side,
            price_cents=candidate.price_cents, contracts=candidate.contracts,
            order_id=order_id, status="accepted", reason="kalshi_ack",
            event_id=candidate.event_id, paper=False,
        )
        _log.info(f"[LIVE] PLACED {po.ticker} {po.side}@{po.price_cents}c x{po.contracts} id={order_id}")
    except Exception as e:
        po = PlacedOrder(
            ticker=candidate.ticker, side=candidate.side,
            price_cents=candidate.price_cents, contracts=candidate.contracts,
            order_id="", status="error", reason=str(e)[:200],
            event_id=candidate.event_id, paper=False,
        )
        _log.error(f"[LIVE] FAILED {po.ticker}: {e}")

    _log_order(db_path, po, candidate.cost_usd(), thesis_payload)
    return po


def place_event_batch(
    theses:      list[dict],    # from thesis_builder (list of thesis_dict)
    event_id:    str,
    bankroll:    float,
    client:      Optional[KalshiOrderClient] = None,
    db_path:     str = "/root/sovereign/data/sovereign.db",
    dry_run:     bool = False,
) -> list[PlacedOrder]:
    """Execute a whole ranked thesis list. Tracks running event exposure so
    G5 (per-event cap) is enforced correctly across the batch."""
    client = client or KalshiOrderClient()
    placed: list[PlacedOrder] = []
    exposure = 0.0
    thesis_generated = datetime.now(timezone.utc)

    for t in theses:
        if t.get("conviction", "SKIP") == "SKIP":
            continue
        # Compute contracts from recommended_usd at chosen side's price
        price_frac = (t["yes_ask"] / 100.0 if t.get("yes_ask") else 0.50)
        if t["best_side"] == "no":
            price_frac = 1.0 - price_frac
        price_cents = max(1, min(99, int(round(price_frac * 100))))

        if t["recommended_usd"] <= 0 or price_cents == 0:
            continue

        contracts = int(t["recommended_usd"] / (price_cents / 100.0))
        if contracts < 1:
            continue

        candidate = OrderCandidate(
            ticker=t["ticker"],
            side=t["best_side"],
            price_cents=price_cents,
            contracts=contracts,
            bankroll_usd=bankroll,
            event_id=event_id,
            event_exposure_usd=exposure,
            thesis_generated_at=thesis_generated,
        )
        po = place_order(candidate, client=client, db_path=db_path,
                         thesis_payload=t, dry_run=dry_run)
        placed.append(po)
        if po.status in ("paper", "accepted"):
            exposure += candidate.cost_usd()

    return placed
