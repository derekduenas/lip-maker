"""Retry-safe client order ids.

Rules: the id is written to disk BEFORE it is returned (so before any send); an
ambiguous response (timeout, 5xx) is resolved by looking the order up by that id and
NEVER by minting a new one; if the lookup itself fails nothing is resent and the id
stays unresolved for reconciliation. The journal is append-only JSONL, replayed on
start, so a crash between send and ack leaves the id visible as unresolved.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from typing import Callable, Optional

TERMINAL = ("acked", "rejected", "absent")


class AmbiguousResponse(Exception):
    """Timeout, connection error or 5xx: the order may or may not exist."""


class RejectedOrder(Exception):
    """The venue definitively refused the order (e.g. post-only would cross)."""


class ClientOrderIds:
    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._state: dict[str, dict] = {}
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(path):
            for line in open(path, encoding="utf-8"):
                try:
                    row = json.loads(line)
                    self._state.setdefault(row["coid"], {}).update(row)
                except (ValueError, KeyError):
                    continue                                    # a torn last line never blocks startup

    def _append(self, row: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._state.setdefault(row["coid"], {}).update(row)

    def new(self, market: str, side: str, prefix: str = "LIP") -> str:
        coid = f"{prefix}-{int(self._clock() * 1000):x}-{secrets.token_hex(6)}"[:64]
        self._append({"coid": coid, "state": "intent", "market": market, "side": side, "ts": self._clock()})
        return coid

    def mark(self, coid: str, state: str, **info) -> None:
        self._append(dict(info, coid=coid, state=state, ts=self._clock()))

    def state(self, coid: str) -> Optional[dict]:
        return self._state.get(coid)

    def unresolved(self) -> list[str]:
        return sorted(c for c, r in self._state.items() if r.get("state") not in TERMINAL)


def submit_idempotent(store: ClientOrderIds, coid: str, send: Callable[[str], dict],
                      lookup: Callable[[str], Optional[dict]], *, max_attempts: int = 3,
                      sleep: Callable[[float], None] = time.sleep, backoff_s: float = 0.5) -> dict:
    """Send with the SAME id until the outcome is known. ``lookup(coid)`` returns the
    venue's order or None when the venue authoritatively has none; it may raise."""
    store.mark(coid, "sent")
    attempts = 0
    while attempts < max_attempts:
        attempts += 1
        try:
            resp = send(coid)
            store.mark(coid, "acked", order_id=(resp or {}).get("order_id", ""))
            return {"status": "acked", "attempts": attempts, "response": resp}
        except RejectedOrder as exc:
            store.mark(coid, "rejected", reason=str(exc)[:120])
            return {"status": "rejected", "attempts": attempts}
        except AmbiguousResponse:
            try:
                found = lookup(coid)
            except Exception:
                store.mark(coid, "unknown", reason="lookup_failed")
                return {"status": "unknown", "attempts": attempts, "reason": "lookup_failed"}
            if found:
                store.mark(coid, "acked", order_id=found.get("order_id", ""))
                return {"status": "acked_after_lookup", "attempts": attempts, "response": found}
            if attempts < max_attempts:
                sleep(backoff_s * (2 ** (attempts - 1)))
    store.mark(coid, "unknown", reason="retries_exhausted")
    return {"status": "unknown", "attempts": attempts, "reason": "retries_exhausted"}
