"""Which PM US book a quote is allowed to use.

Live check, 1 October 2026, against the signed REST order book on
api.polymarket.us: Cloudflare returned cache HIT with age 15–16s. The
cache is about 30 seconds, and the signature did not bypass it. The
signed websocket market channel pushed the full book about ten times a
second. The median message was 0.16s old.

A maker quote uses that websocket book. A REST book is stale and is not
a quote. A websocket book older than one second is a pull: cancel the
resting quote and wait for a fresh push.
"""
from __future__ import annotations

WS_QUOTE_MAX_AGE_S = 1.0


def ws_book_is_quotable(age_s: float | None) -> bool:
    """True only for a websocket book that is not older than one second."""
    if age_s is None:
        return False
    return float(age_s) <= WS_QUOTE_MAX_AGE_S


def book_for_quote(*, source: str, book: dict | None, now: float,
                   updated_at: float | None) -> tuple[dict | None, str]:
    """Return ``(book, "quote")`` or ``(None, "pull")``.

    ``source`` must be ``"ws"``. ``"rest"`` is a pull even when the
    payload is signed and the timestamp looks new.
    """
    if source != "ws" or not book or updated_at is None:
        return None, "pull"
    age = float(now) - float(updated_at)
    if not ws_book_is_quotable(age):
        return None, "pull"
    quoted = {
        "best_bid": book.get("best_bid"),
        "best_ask": book.get("best_ask"),
        "top_bid_size": book.get("top_bid_size"),
        "top_ask_size": book.get("top_ask_size"),
        "source": "ws",
        "age_s": age,
    }
    if quoted["best_bid"] is None or quoted["best_ask"] is None:
        return None, "pull"
    return quoted, "quote"


def quote_book(*, ws_book: dict | None, ws_updated_at: float | None,
               rest_book: dict | None, now: float) -> tuple[dict | None, str]:
    """Websocket book, or a pull. ``rest_book`` is not read."""
    del rest_book
    return book_for_quote(source="ws", book=ws_book, now=now, updated_at=ws_updated_at)
