"""Balance poller — auto-detect Apr 28 LIP payout landing.

Polls Kalshi /portfolio/balance every 5 minutes, logs each reading, and
prints a clear ALERT line when balance jumps by >$100 (LIP payout signal).

Designed to run as a one-shot for Tuesday's payout:
  python tools/balance_poller.py --duration-hours 12 --threshold 100

Or as a long-lived monitor:
  python tools/balance_poller.py --duration-hours 24

Output goes to stdout + logs/balance_poller.log. ALERT lines are
greppable via 'grep ALERT logs/balance_poller.log'.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.kalshi_auth import KalshiClient

_log = logging.getLogger(__name__)


def get_balance_usd(client: KalshiClient) -> float | None:
    """Return USD cash balance from Kalshi /portfolio/balance, None on error."""
    try:
        r = client.get("/portfolio/balance")
        # Kalshi returns balance in cents
        cents = r.get("balance", 0)
        return cents / 100.0
    except Exception as e:
        _log.warning(f"balance fetch failed: {e}")
        return None


def poll_loop(duration_hours: float, interval_min: float, threshold_usd: float) -> int:
    client = KalshiClient()
    end_ts = time.time() + duration_hours * 3600
    iteration = 0
    last_balance: float | None = None
    payout_detected = False

    while time.time() < end_ts:
        iteration += 1
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        balance = get_balance_usd(client)
        if balance is None:
            _log.warning(f"[{now}] poll #{iteration}: balance unavailable")
        else:
            if last_balance is None:
                _log.info(f"[{now}] poll #{iteration}: baseline ${balance:,.2f}")
            else:
                delta = balance - last_balance
                if abs(delta) >= threshold_usd:
                    payout_detected = True
                    _log.warning(
                        f"[{now}] 🚨 ALERT poll #{iteration}: "
                        f"balance JUMPED ${delta:+,.2f} "
                        f"(${last_balance:,.2f} → ${balance:,.2f}). "
                        f"Likely LIP payout."
                    )
                else:
                    _log.info(
                        f"[{now}] poll #{iteration}: ${balance:,.2f} "
                        f"(Δ ${delta:+,.2f})"
                    )
            last_balance = balance

        time.sleep(interval_min * 60)

    if payout_detected:
        _log.info("session ended; payout detected at least once.")
        return 0
    _log.info("session ended; no payout-sized jump detected during window.")
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--duration-hours", type=float, default=12.0,
                   help="how long to keep polling (default: 12h)")
    p.add_argument("--interval-min", type=float, default=5.0,
                   help="minutes between polls (default: 5)")
    p.add_argument("--threshold", type=float, default=100.0,
                   help="USD balance jump that triggers ALERT (default: 100)")
    a = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("logs/balance_poller.log"),
        ],
    )
    return poll_loop(
        duration_hours=a.duration_hours,
        interval_min=a.interval_min,
        threshold_usd=a.threshold,
    )


if __name__ == "__main__":
    sys.exit(main())
