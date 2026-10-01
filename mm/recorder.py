"""Append-only JSONL recorder for books, trades and quotes.

Replay reads the same file. A record is one JSON object per line with a
``kind`` field. The writer does not buffer across calls: a killed process
should not lose the last book.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, TextIO


class Recorder:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: TextIO | None = None

    def _file(self) -> TextIO:
        if self._fh is None:
            self._fh = self.path.open("a", encoding="utf-8")
        return self._fh

    def write(self, kind: str, **payload) -> None:
        row = {"kind": kind, **payload}
        fh = self._file()
        fh.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
        fh.flush()

    def book(self, *, ts: float, market: str, yes_bid: int, no_bid: int,
             yes_size: float = 0, no_size: float = 0) -> None:
        self.write("book", ts=ts, market=market, yes_bid=yes_bid, no_bid=no_bid,
                   yes_size=yes_size, no_size=no_size)

    def quote(self, *, ts: float, market: str, side: str, price_cents: int,
              size: float, order_id: str) -> None:
        self.write("quote", ts=ts, market=market, side=side, price_cents=price_cents,
                   size=size, order_id=order_id)

    def trade(self, trade: dict) -> None:
        self.write("trade", trade=trade)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def read_records(path: str | Path) -> Iterable[dict]:
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)
