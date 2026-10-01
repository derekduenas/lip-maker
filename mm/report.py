"""One-page daily report. Cash and estimates are printed in different lines."""
from __future__ import annotations

from decimal import Decimal

from mm.replay import ReplayResult


def render_daily(result: ReplayResult, *, day: str) -> str:
    lines = [
        f"daily report {day}",
        f"fills {len(result.fills)}",
        f"cash_pnl_usd {result.cash_pnl_usd:.4f}",
        f"  realized_plus_paid_plus_rebate_minus_fees (estimates excluded)",
        f"fees_usd {result.fees_usd:.4f}",
        f"rebates_usd {result.rebates_usd:.4f}",
        f"estimated_reward_usd {result.estimated_reward_usd:.4f}",
        "markets:",
    ]
    for market, book in sorted(result.books.rows.items()):
        lines.append(
            f"  {market} cash {book.cash_pnl_usd:.4f} "
            f"estimated {book.estimated_usd:.4f} paid {book.paid_usd:.4f} "
            f"provenance {book.provenance}"
        )
    if not result.books.rows:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"


def write_daily(path: str, result: ReplayResult, *, day: str) -> str:
    text = render_daily(result, day=day)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text
