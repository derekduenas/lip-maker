"""Continuous depth re-probe — audit fix #2 follow-on.

For each currently-resting order, fetch live orderbook, compute our
projected share AT OUR RESTING PRICE. If competition has stacked up
since we placed the order and our share dropped below the watermark,
CANCEL the order. Engine's next allocation cycle will redeploy
elsewhere via depth_probe pre-filter.

This is the "re-probe every 60s while resting" half of audit item #2.
The deploy-time depth gate (Sprint 4 #2) handles the entry side; this
handles drift after entry.

Cron every 60s. Cancel-only — never re-place from this tool (avoids
race with engine's quote_manager).

Usage:
    python -m tools.depth_recheck                # log+cancel below watermark
    python -m tools.depth_recheck --dry-run      # log only
    python -m tools.depth_recheck --watermark 0.03   # min share threshold
"""
from __future__ import annotations
import argparse
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from execution.kalshi_auth import KalshiClient
from engine.depth_probe import probe, projected_share

# Cron heartbeat (atexit pattern matches other crons)
import atexit as _atexit
try:
    from tools._heartbeat import write_heartbeat as _wh
    _atexit.register(_wh, "depth_recheck")
except Exception:
    pass

_log = logging.getLogger(__name__)


WATERMARK_DEFAULT = 0.03  # 3% — looser than entry gate (5%) for hysteresis
SLEEP_BETWEEN_PROBES = 0.05


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watermark", type=float, default=WATERMARK_DEFAULT,
                    help="Cancel if projected share at resting price < watermark")
    ap.add_argument("--dry-run", action="store_true",
                    help="Audit only — do not cancel")
    ap.add_argument("--max-orders", type=int, default=80,
                    help="Cap probes per cycle (Kalshi rate limit safety)")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    client = KalshiClient()

    # Pull all resting orders
    try:
        r = client.get("/portfolio/orders", params={"status": "resting", "limit": 200})
        orders = r.get("orders", [])
    except Exception as e:
        _log.error(f"failed to fetch resting orders: {e}")
        return 1

    if not orders:
        _log.info("no resting orders to recheck")
        return 0

    # Group orders by ticker for one-probe-per-ticker
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for o in orders:
        by_ticker[o.get("ticker", "")].append(o)

    n_probed = 0
    n_cancelled = 0
    n_kept = 0
    n_errors = 0

    for ticker, ticker_orders in by_ticker.items():
        if n_probed >= args.max_orders:
            break
        if not ticker:
            continue

        d = probe(client, ticker)
        n_probed += 1
        time.sleep(SLEEP_BETWEEN_PROBES)
        if not d.get("ok"):
            n_errors += 1
            continue

        for o in ticker_orders:
            side = o.get("side", "")
            # Kalshi uses fixed-point string fields (e.g. remaining_count_fp="60.00")
            try:
                rem = int(float(o.get("remaining_count_fp", o.get("remaining_count", 0) or 0)))
            except (ValueError, TypeError):
                rem = 0
            if rem <= 0:
                continue

            top_depth = d["yes_size"] if side == "yes" else d["no_size"]
            share = projected_share(rem, top_depth)

            if share < args.watermark:
                action = "DRY-CANCEL" if args.dry_run else "CANCEL"
                _log.info(
                    f"{action} {ticker} {side} rem={rem} top_depth={top_depth:.0f} "
                    f"share={share*100:.2f}% < {args.watermark*100:.1f}%"
                )
                if not args.dry_run:
                    oid = o.get("order_id", "")
                    try:
                        # Kalshi cancel = DELETE /portfolio/orders/{id}
                        # (matches quote_manager._cancel_order pattern)
                        client.delete(f"/portfolio/orders/{oid}")
                        n_cancelled += 1
                    except Exception as e:
                        _log.warning(f"cancel failed for {ticker} {side} oid={oid[:8]}: {e}")
                        n_errors += 1
            else:
                n_kept += 1

    print(f"depth_recheck: probed={n_probed} kept={n_kept} cancelled={n_cancelled} "
          f"errors={n_errors} watermark={args.watermark*100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
