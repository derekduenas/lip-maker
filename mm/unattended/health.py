"""Heartbeat, daily summary, webhook alert, and the reward/markout kill."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from mm.safety.supervisor import write_heartbeat

WINDOW_SECONDS = 86400.0


ATTRIBUTION_KEYS = ("spread_capture_usd", "adverse_selection_usd", "inventory_mtm_usd",
                    "est_rewards_kalshi_usd", "est_rewards_pmus_usd", "rebates_usd", "fees_usd")


def render_daily_summary(*, day: str, fills: int, pnl_usd: float, rewards_usd: float,
                         data_source: str | None = None, buckets: dict | None = None,
                         premium_paid_usd: float | None = None,
                         attribution: dict | None = None) -> str:
    """``pnl_usd`` is the engine's estimated P&L (MTM markout + estimated
    rewards + rebates - fees); ``premium_paid_usd`` is what fills cost;
    ``rewards_usd`` is paid/inferred rewards only. ``attribution``
    (RunLoop.pnl_attribution) splits pnl_usd into estimated parts that sum
    to it; it is printed as ``pnl_attribution_estimate_paper``."""
    text = (
        f"daily summary {day}\n"
        f"fills {int(fills)}\n"
        f"pnl_usd {float(pnl_usd):.4f}\n"
        f"rewards_usd {float(rewards_usd):.4f}\n"
    )
    if premium_paid_usd is not None:
        text += f"premium_paid_usd {float(premium_paid_usd):.4f}\n"
    if data_source:
        text += f"data_source {data_source}\n"
    if attribution:
        text += "pnl_attribution_estimate_paper " + " ".join(
            f"{k} {float(attribution.get(k) or 0.0):.4f}" for k in ATTRIBUTION_KEYS) + "\n"
    for name, b in sorted((buckets or {}).items()):
        text += (f"bucket {name} selected {int(b.get('selected_n', 0))} "
                 f"capital_usd {float(b.get('capital_usd', 0)):.2f} "
                 f"raw_est_rewards_usd {float(b.get('raw_est_usd', 0)):.4f} "
                 f"fills {int(b.get('fills_n', 0))} "
                 f"markout_usd {float(b.get('markout_usd', 0)):.4f} "
                 f"fees_usd {float(b.get('fees_usd', 0)):.4f} "
                 f"pnl_usd {float(b.get('pnl_usd', 0)):.4f}\n")
    return text


class Health:
    """Rolling 24h reward-to-markout kill switch.

    Ratio is reward dollars divided by markout cost dollars. Below 1, the
    book is paying us less than it is costing. The switch cancels and
    stays latched. Samples older than 24 hours leave the ratio.
    """

    def __init__(self, heartbeat: str | Path | None, cancel: Callable[[], None],
                 alert: Callable[[dict], None], *,
                 webhook_url: Optional[str] = None) -> None:
        self.heartbeat = heartbeat
        self.cancel = cancel
        self.alert = alert
        if webhook_url is None:
            webhook_url = os.environ.get("LIP_ALERT_WEBHOOK") or ""
        self.webhook_url = webhook_url or ""
        self.killed = False
        self._samples: list[tuple[float, float, float]] = []

    def reset(self) -> None:
        """Clear the latch. Samples stay, so a still-open breach can trip again."""
        self.killed = False

    def beat(self, now: float | None = None) -> None:
        if self.heartbeat is not None:
            write_heartbeat(self.heartbeat, now=now)

    def ratio(self, now: float) -> float | None:
        reward = 0.0
        cost = 0.0
        seen = False
        for ts, sample_reward, sample_cost in self._samples:
            if ts <= float(now) - WINDOW_SECONDS or ts > float(now):
                continue
            seen = True
            reward += sample_reward
            cost += sample_cost
        if not seen or cost <= 0:
            return None
        return reward / cost

    def record(self, ts: float, *, reward_usd: float, markout_cost_usd: float) -> None:
        self._samples.append((float(ts), float(reward_usd), float(markout_cost_usd)))
        if self.killed:
            return
        current = self.ratio(ts)
        if current is None or current >= 1.0:
            return
        self.killed = True
        self.cancel()
        if not self.webhook_url:
            return
        self.alert({
            "reason": "reward_markout_ratio",
            "ratio": current,
            "webhook": self.webhook_url,
        })
