"""Fair value from an external reference, plus a pull when that reference jumps.

The book mid is what the crowd last traded. It is a stale fair value when
the underlying has already moved (a Brent print, a BTC spot, an NWS
observation). This module does not invent a probability when it lacks a
volatility. With a horizon sigma it uses a Bachelier digital:

    P(S_T >= K) = Φ((S - K) / (σ √τ))

σ and τ are in the same units the caller supplies (price units, and years
or hours — they must match). Without σ the reference is used only as a
*move* trigger: if spot has jumped more than ``k`` times the last observed
tick, quotes should be pulled. That is a circuit breaker, not a price.

Family tags drive pool selection. They are prefix conventions from this
repo's series tickers, not an exchange taxonomy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


_COMMODITY = ("KXBRENT", "KXGOLD", "KXSILVER", "KXCOPPER", "KXCORN", "KXSOY",
              "KXWHEAT", "KXCOCOA", "KXNATGAS", "KXCRUDE", "KXPALL", "KXPLAT")
_CRYPTO = ("KXBTC", "KXETH", "KXCRYPTO", "KXSOL", "KXDOGE")
_WEATHER = ("KXHIGH", "KXLOWT", "KXTEMP", "KXRAIN")


def family_for_series(series: str) -> str:
    s = (series or "").upper()
    if s.startswith(_COMMODITY):
        return "commodity"
    if s.startswith(_CRYPTO):
        return "crypto"
    if s.startswith(_WEATHER):
        return "weather"
    return "event"


def series_of(ticker: str) -> str:
    return (ticker or "").split("-", 1)[0].upper()


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class Reference:
    """One external print for one market."""
    symbol: str
    spot: float
    prev_spot: Optional[float]
    strike: Optional[float]
    # σ in the same price units as spot, already scaled by √τ
    # (i.e. the denominator of the digital is this number).
    sigma_horizon: Optional[float] = None
    # Weather: latest observation minus the contract's forecast, in the
    # contract's units (degrees). None when we have not seen an observation.
    observation_gap: Optional[float] = None
    has_observation: bool = False


_REFS: dict[str, "Reference"] = {}


def register_reference(ticker: str, ref: Optional["Reference"]) -> None:
    """Install an external print for one market. ``None`` clears it."""
    if ref is None:
        _REFS.pop(ticker, None)
    else:
        _REFS[ticker] = ref


def lookup_reference(ticker: str) -> Optional["Reference"]:
    return _REFS.get(ticker)


def clear_references() -> None:
    _REFS.clear()


@dataclass(frozen=True)
class Fair:
    yes_cents: float
    source: str                 # book | digital | observation
    family: str
    pull: bool
    pull_reason: str = ""


def digital_yes_cents(spot: float, strike: float, sigma_horizon: float) -> float:
    """Bachelier probability, in cents, that spot finishes at or above strike.

    sigma_horizon <= 0 is refused: a zero-vol digital is a step function we
    would be pretending to know.
    """
    if sigma_horizon <= 0:
        raise ValueError("sigma_horizon must be positive")
    p = _phi((float(spot) - float(strike)) / float(sigma_horizon))
    return max(1.0, min(99.0, 100.0 * p))


def fair_yes(ticker: str, book_mid_cents: Optional[float], ref: Optional[Reference], *,
             move_k: float = 3.0, min_move: float = 0.0) -> Fair:
    """Blend a reference into the book, or pull if the reference just jumped.

    ``move_k`` is in units of ``sigma_horizon`` when that is set, otherwise
    a jump larger than ``min_move`` (absolute spot units) pulls. If neither
    a sigma nor a min_move is available, a reference without a model does
    not pull and does not replace the book.
    """
    fam = family_for_series(series_of(ticker))
    if ref is None or book_mid_cents is None:
        return Fair(yes_cents=float(book_mid_cents or 0.0), source="book",
                    family=fam, pull=book_mid_cents is None,
                    pull_reason="" if book_mid_cents is not None else "no_book")
    if ref.prev_spot is not None and ref.spot != ref.prev_spot:
        jumped = abs(ref.spot - ref.prev_spot)
        threshold = None
        if ref.sigma_horizon and ref.sigma_horizon > 0:
            threshold = move_k * ref.sigma_horizon
        elif min_move > 0:
            threshold = min_move
        if threshold is not None and jumped > threshold:
            return Fair(yes_cents=float(book_mid_cents), source="book", family=fam,
                        pull=True,
                        pull_reason=f"external_move {ref.symbol} {jumped:.6g} > {threshold:.6g}")
    if (ref.strike is not None and ref.sigma_horizon is not None
            and ref.sigma_horizon > 0):
        model = digital_yes_cents(ref.spot, ref.strike, ref.sigma_horizon)
        # The book still has queue and fee information the digital ignores.
        # Half weight each, so a quiet book is not ignored and a moved spot
        # is not ignored either.
        blended = 0.5 * model + 0.5 * float(book_mid_cents)
        return Fair(yes_cents=max(1.0, min(99.0, blended)), source="digital",
                    family=fam, pull=False)
    if fam == "weather" and ref.has_observation and ref.observation_gap is not None:
        # A large observation gap means the contract's forecast is stale.
        # We do not map degrees to cents without a local climatology; we pull.
        if abs(ref.observation_gap) >= 2.0:
            return Fair(yes_cents=float(book_mid_cents), source="observation",
                        family=fam, pull=True,
                        pull_reason=f"weather_obs_gap {ref.observation_gap:+.2f}")
    return Fair(yes_cents=float(book_mid_cents), source="book", family=fam, pull=False)
