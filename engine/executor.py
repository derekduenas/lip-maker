"""Executor — places LIMIT orders via Kalshi API. Paper mode by default."""

import math
import sqlite3
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import (
    DB_PATH, PAPER_MODE, SHADOW_MODE, ORDER_TYPE, KELLY_FRACTION,
    MAX_TRADE_PCT, MAX_DAILY_DEPLOY, MIN_CONTRACTS, MAX_PRICE_CHASE,
    DAILY_LOSS_CAP_PCT,
)


def _get_conn():
    return sqlite3.connect(DB_PATH)


class Executor:
    """Places orders — paper or live."""

    def __init__(self, bankroll: float = None):
        self.paper = PAPER_MODE
        if bankroll is None:
            # Try to read from Kalshi balance
            try:
                from engine.scanner import KalshiClient
                client = KalshiClient()
                if client.authenticated:
                    data = client._get("/portfolio/balance")
                    self.bankroll = float(data.get("balance", 0)) / 100.0  # cents -> dollars
                else:
                    self.bankroll = 100.0  # default paper bankroll
            except Exception:
                self.bankroll = 100.0
        else:
            self.bankroll = bankroll

    def size_position(self, kelly_fraction: float, price: float) -> int:
        """
        Returns number of contracts to buy.
        contracts = floor((bankroll * kelly_fraction) / price)
        Enforces MAX_TRADE_PCT hard cap.
        Minimum: MIN_CONTRACTS (below this, fees eat the edge).
        """
        deploy_pct = min(kelly_fraction, MAX_TRADE_PCT)
        dollar_amount = self.bankroll * deploy_pct
        if price <= 0:
            return 0
        contracts = math.floor(dollar_amount / price)
        if contracts < MIN_CONTRACTS:
            return 0  # not enough to overcome fees
        return contracts

    def place_limit_order(
        self,
        ticker: str,
        term: str,
        side: str,
        price_cents: int,
        contracts: int,
        opportunity_id: int = None,
        paper: bool = None,
    ) -> dict:
        """
        Place a LIMIT order — paper or live.

        If paper=True: log to DB, print, do NOT call Kalshi API.
        If paper=False: call POST /portfolio/orders, then log to DB.
        """
        if paper is None:
            paper = self.paper

        price_dollars = price_cents / 100.0
        total_cost = price_dollars * contracts
        from engine.edge_calc import kalshi_maker_fee
        fee = kalshi_maker_fee(price_dollars) * contracts

        trade = {
            "ticker": ticker,
            "term": term,
            "side": side.lower(),
            "price_cents": price_cents,
            "price_dollars": price_dollars,
            "contracts": contracts,
            "total_cost": total_cost,
            "fee": fee,
            "paper": paper,
            "opportunity_id": opportunity_id,
        }

        # Shadow mode: log decision but don't place any order
        if SHADOW_MODE:
            mode = "SHADOW"
            trade["order_id"] = f"SHADOW-{ticker}-{int(datetime.now(timezone.utc).timestamp())}"
            conn = _get_conn()
            conn.execute(
                """INSERT INTO shadow_trades
                   (kalshi_market_id, term, side, price, contracts, total_cost, edge,
                    confidence_weight, strategy, estimated_prob, market_price)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ticker, term, side.upper(), price_dollars, contracts, total_cost,
                 None, None, None, None, price_dollars)
            )
            conn.commit()
            conn.close()
            print(f"  [{mode}] BUY {contracts}x {ticker} {side.upper()} @{price_cents}c"
                  f"  | cost=${total_cost:.2f} (shadow — no order placed)")
            return trade

        if paper:
            mode = "PAPER"
            trade["order_id"] = f"PAPER-{ticker}-{int(datetime.now(timezone.utc).timestamp())}"
        else:
            mode = "LIVE"
            # Place real order via Kalshi API
            try:
                from engine.scanner import KalshiClient
                client = KalshiClient()
                if not client.authenticated or not client._private_key:
                    raise RuntimeError("Kalshi API not authenticated — cannot place live orders")

                # Build order payload
                order_payload = {
                    "action": "buy",
                    "side": side.lower(),
                    "type": "limit",
                    "count": contracts,
                    "ticker": ticker,
                }
                # Kalshi expects yes_price or no_price in cents
                if side.lower() == "yes":
                    order_payload["yes_price"] = price_cents
                else:
                    order_payload["no_price"] = price_cents

                import json, time as _time
                from base64 import b64encode
                from cryptography.hazmat.primitives import hashes
                from cryptography.hazmat.primitives.asymmetric import padding

                path = "/trade-api/v2/portfolio/orders"
                body = json.dumps(order_payload)
                ts = str(int(_time.time() * 1000))
                msg = f"{ts}POST{path}"

                sig = client._private_key.sign(
                    msg.encode("utf-8"),
                    padding.PSS(
                        mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH,
                    ),
                    hashes.SHA256(),
                )

                headers = {
                    "KALSHI-ACCESS-KEY": client.api_key,
                    "KALSHI-ACCESS-TIMESTAMP": ts,
                    "KALSHI-ACCESS-SIGNATURE": b64encode(sig).decode("utf-8"),
                    "Content-Type": "application/json",
                }

                import requests as _requests
                resp = _requests.post(
                    f"{client.base_url}/portfolio/orders",
                    headers=headers,
                    data=body,
                    timeout=30,
                )
                resp.raise_for_status()
                order_data = resp.json()
                trade["order_id"] = order_data.get("order", {}).get("order_id", "LIVE-UNKNOWN")
                trade["kalshi_response"] = order_data

            except Exception as e:
                print(f"  [LIVE ORDER FAILED] {e}")
                raise

        # Log to DB
        conn = _get_conn()
        conn.execute(
            """INSERT INTO trades
               (opportunity_id, kalshi_market_id, term, side, order_type,
                price, contracts, total_cost, fee_paid, paper, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                opportunity_id,
                ticker,
                term,
                side.upper(),
                ORDER_TYPE,
                price_dollars,
                contracts,
                total_cost,
                fee,
                paper,
                "OPEN",
            )
        )
        conn.commit()

        trade_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        trade["trade_id"] = trade_id

        print(f"  [{mode}] BUY {contracts}x {ticker} {side.upper()} @{price_cents}c"
              f"  | cost=${total_cost:.2f} | fee=${fee:.4f}")

        return trade

    def get_maker_price(self, ticker: str, side: str, mid_price: float) -> tuple[int, dict]:
        """
        Fetch orderbook and compute optimal maker entry price.
        Returns (price_cents, liquidity_info).
        For BUY YES: sit at best_bid - 1c (below book)
        For BUY NO: sit at best_no_bid - 1c
        """
        liquidity = {"grade": "unknown", "spread": 0, "depth": 0}

        try:
            from engine.scanner import KalshiClient
            client = KalshiClient()
            if not client.authenticated:
                return int(round(mid_price * 100)), liquidity

            raw = client._get(f"/markets/{ticker}")
            m = raw.get("market", raw)
            yes_ask = float(m.get("yes_ask_dollars", 0) or 0)
            yes_bid = float(m.get("yes_bid_dollars", 0) or 0)

            if yes_ask > 0 and yes_bid > 0:
                spread = yes_ask - yes_bid
                liquidity["spread"] = spread

                if side.lower() == "yes":
                    # Sit 1c below current best bid to be maker
                    maker_price = max(0.01, yes_bid - 0.01)
                    price_cents = int(round(maker_price * 100))
                else:
                    # For NO side: we want to buy NO, so sit below the NO bid
                    no_bid = 1.0 - yes_ask  # NO bid = 1 - YES ask
                    maker_price = max(0.01, no_bid - 0.01)
                    price_cents = int(round(maker_price * 100))

                # Grade liquidity
                if spread <= 0.03:
                    liquidity["grade"] = "thick"
                elif spread <= 0.06:
                    liquidity["grade"] = "medium"
                else:
                    liquidity["grade"] = "thin"

                discount = mid_price - (price_cents / 100.0)
                liquidity["entry_discount_pp"] = round(discount * 100, 1)

                return price_cents, liquidity

        except Exception as e:
            pass  # fallback to mid price

        return int(round(mid_price * 100)), liquidity

    def _today_realized_pnl(self) -> float:
        """Sum of PnL on trades resolved today (UTC date). Paper + live both count."""
        try:
            conn = _get_conn()
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                "WHERE outcome IN ('WIN','LOSS') AND DATE(resolved_at) = ?",
                (today,),
            ).fetchone()
            conn.close()
            return float(row[0] or 0.0)
        except Exception:
            return 0.0

    def execute_opportunity(self, opportunity: dict) -> dict:
        """
        Takes an opportunity dict and executes the trade.
        Sizes position using maker-optimized entry price.
        """
        # Daily circuit breaker — halt trading for the day if today's realized
        # PnL <= -DAILY_LOSS_CAP_PCT * bankroll. Added 2026-04-19; Sovereign was
        # missing a daily loss cap (Predator has one at -$18).
        cap_usd = -abs(DAILY_LOSS_CAP_PCT) * self.bankroll
        today_pnl = self._today_realized_pnl()
        if today_pnl <= cap_usd:
            print(f"  [CIRCUIT BREAKER] today_pnl=${today_pnl:+.2f} <= cap=${cap_usd:+.2f}; halting day")
            return {"skipped": True, "reason": f"daily_loss_cap:${today_pnl:+.2f}<=${cap_usd:+.2f}"}

        side = opportunity.get("trade_side", opportunity.get("side", "yes"))
        if side.upper() == "NO":
            mid_price = 1.0 - opportunity["market_price"]
        else:
            mid_price = opportunity["market_price"]

        # Get maker-optimized price
        ticker = opportunity.get("ticker", "")
        maker_price_cents, liquidity = self.get_maker_price(ticker, side, mid_price)
        price = maker_price_cents / 100.0

        kelly = opportunity.get("kelly_fraction", opportunity.get("kelly", 0.0))

        # Apply liquidity grade multiplier
        liq_grade = liquidity.get("grade", "unknown")
        if liq_grade == "thin":
            kelly = 0.0  # skip thin markets entirely
        elif liq_grade == "medium":
            kelly *= 0.7

        contracts = self.size_position(kelly, price)

        if contracts == 0:
            reason = "thin_liquidity" if liq_grade == "thin" else "position_too_small"
            print(f"  [SKIP] {opportunity['term']}: {reason} "
                  f"(kelly={kelly*100:.1f}%, spread={liquidity.get('spread',0)*100:.0f}c)")
            return {"skipped": True, "reason": reason}

        result = self.place_limit_order(
            ticker=ticker,
            term=opportunity.get("term", ""),
            side=side,
            price_cents=maker_price_cents,
            contracts=contracts,
            opportunity_id=opportunity.get("opportunity_id"),
        )

        if not result.get("skipped"):
            result["liquidity_grade"] = liq_grade
            result["entry_discount_pp"] = liquidity.get("entry_discount_pp", 0)
            if liquidity.get("entry_discount_pp", 0) > 0:
                print(f"    Maker entry: {liquidity['entry_discount_pp']:+.1f}pp better than mid")

        return result


if __name__ == "__main__":
    executor = Executor()
    print(f"Executor initialized. Bankroll: ${executor.bankroll:.2f}, Paper: {executor.paper}")
