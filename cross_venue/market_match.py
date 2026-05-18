"""HedgeMap — Kalshi ticker prefix → external hedge instrument.

Pure data module. No I/O. Consumed by `cross_venue.hedger` to compute
how many units of a CME/Kraken/ICE instrument to trade against a given
Kalshi inventory.

THE INSIGHT
-----------
A Kalshi YES contract on a strike K pays $1 if the underlying settles
above K (binary call payoff). Holding N YES contracts is approximately
N × ΔBinaryCall worth of underlying delta. As spot moves through K the
delta peaks near 1/(σ√(2π·(T−t))) per unit notional and the position
behaves like a digital call.

To neutralize, we short a continuous futures contract sized so:

    hedge_qty_units = N_kalshi × $payoff × Δ(binary) / contract_value

Where Δ(binary) ≈ φ(d2) / (S · σ · √(T−t)) and contract_value is
the dollar delta of one futures contract per $1 of spot move.

For practical use the function below uses a closed-form approximation
based on the standard normal CDF. Per-mapping σ defaults are seeded
from realized volatility observations; the operator can override.

USAGE
-----
    from cross_venue.market_match import HEDGE_MAP, hedge_for_ticker

    hedge = hedge_for_ticker("KXBRENTD-26JUN0117-T100")
    if hedge:
        n_futures = hedge.hedge_qty(
            kalshi_contracts=200,
            strike_price=100.0,
            spot=98.50,
            hours_to_settle=12,
        )
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Optional


# ── Standard normal CDF & PDF (no scipy dependency) ──────────────────────────

def _phi(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _Phi(x: float) -> float:
    """Standard normal CDF using erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ── Binary-call delta approximation ──────────────────────────────────────────

def binary_delta(spot: float, strike: float, sigma_annual: float,
                 years_to_settle: float) -> float:
    """Delta of a cash-or-nothing binary call paying $1 if spot > strike at expiry.

    Δ = φ(d2) / (spot × σ × √(T−t))

    where d2 = (ln(S/K) − ½σ²T) / (σ√T). Returns the change in option
    value per unit change in spot, in dollars per dollar.

    Args:
        spot: current underlying price
        strike: option strike
        sigma_annual: annualized volatility, e.g. 0.30 = 30%
        years_to_settle: T − t in years (clamped to a tiny floor)
    """
    t = max(1.0 / (365.0 * 24.0 * 60.0), float(years_to_settle))   # ≥ 1 min
    s = max(1e-6, float(spot))
    k = max(1e-6, float(strike))
    sig = max(1e-4, float(sigma_annual))
    sig_root_t = sig * math.sqrt(t)
    d1 = (math.log(s / k) + 0.5 * sig * sig * t) / sig_root_t
    d2 = d1 - sig_root_t
    return _phi(d2) / (s * sig_root_t)


# ── HedgeSpec ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HedgeSpec:
    """How to hedge a Kalshi position with an external instrument.

    Attributes:
        instrument: external symbol (e.g. CME "MCL", Kraken "XBTUSD", PM slug)
        hedge_venue: "CME" | "Kraken" | "ICE" | "Polymarket" | "Hyperliquid" | "manual"
        contract_size_units: how much underlying one external contract covers
            (e.g. 100 bbl for MCL, 1 BTC for Kraken perp, 1 contract for PM)
        contract_unit: human label ("bbl", "MMBtu", "BTC", "$/oz", "contracts")
        sigma_annual_default: annualized vol prior for delta approximation
        kalshi_payoff_usd: dollar payoff per Kalshi contract (Kalshi binary = $1)
        hedge_strategy: how to compute hedge qty
            "delta_binary": classic delta hedge against continuous underlying
                            (Kraken/CME/ICE). Requires strike + spot + sigma.
            "same_event_unit": 1:1 cross-venue same-event hedge (Polymarket).
                               Bypass delta math; qty = -kalshi_contracts.
        notes: per-mapping caveats (e.g. confidence, contract-month, basis)
    """
    instrument: str
    hedge_venue: str
    contract_size_units: float
    contract_unit: str
    sigma_annual_default: float = 0.30
    kalshi_payoff_usd: float = 1.0
    hedge_strategy: str = "delta_binary"
    notes: str = ""

    def hedge_qty(self, kalshi_contracts: int, strike_price: float,
                  spot: float, hours_to_settle: float,
                  sigma_annual: Optional[float] = None) -> float:
        """Return signed external contract qty needed to hedge a Kalshi position.

        Routing depends on hedge_strategy:
          delta_binary (default): full delta-hedge math (Kraken/CME/ICE)
          same_event_unit: 1:1 opposite-side on the SAME event (Polymarket).
                           Returns -kalshi_contracts directly.

        Sign convention: positive kalshi_contracts = long YES on Kalshi.
        delta_binary returns negative qty (short underlying to flatten).
        same_event_unit returns -kalshi_contracts (buy NO of same event,
        since YES_kalshi + NO_pm = $1 guaranteed payoff).
        """
        if kalshi_contracts == 0 or self.contract_size_units <= 0:
            return 0.0
        if self.hedge_strategy == "same_event_unit":
            # 1:1 cross-venue hedge. PM contract = 1 contract face value.
            # Long YES on Kalshi → BUY NO on PM (same magnitude, opposite outcome).
            # The hedger downstream maps the negative sign to side='no'/'sell'.
            return -float(kalshi_contracts)
        # Default: delta-binary continuous-underlying hedge
        sig = float(sigma_annual) if sigma_annual is not None else self.sigma_annual_default
        years = max(1.0 / (365.0 * 24.0), float(hours_to_settle) / (365.0 * 24.0))
        delta_binary = binary_delta(spot, strike_price, sig, years)
        return -(kalshi_contracts * self.kalshi_payoff_usd * delta_binary
                 / self.contract_size_units)


# ── Mapping table ────────────────────────────────────────────────────────────
#
# Keyed by Kalshi SERIES PREFIX (text before the first "-" in a ticker).
# Example: KXBRENTD matches "KXBRENTD-26JUN0117-T100".
#
# Source for FUTURES symbols + confidence: engine/futures_feed.py:FUTURES_MAP
# (Kalshi 'rules_primary' field verification 2026-04-21). Crypto instruments
# from Kraken Pro perp universe; USD/contract unit normalized.
HEDGE_MAP: dict[str, HedgeSpec] = {
    # ── Energy commodities (CME / NYMEX micros) ──────────────────────────
    "KXCRUDE":   HedgeSpec(
        instrument="MCL", hedge_venue="CME",
        contract_size_units=100.0, contract_unit="bbl",
        sigma_annual_default=0.35,
        notes="Micro WTI Crude, 100 bbl/contract, 1 USD/bbl tick = $1",
    ),
    "KXBRENTD":  HedgeSpec(
        instrument="BZ", hedge_venue="ICE",
        contract_size_units=1000.0, contract_unit="bbl",
        sigma_annual_default=0.32,
        notes="ICE Brent (no micro available); 1000 bbl/contract — sizing minima",
    ),
    "KXBRENTW":  HedgeSpec(
        instrument="BZ", hedge_venue="ICE",
        contract_size_units=1000.0, contract_unit="bbl",
        sigma_annual_default=0.32,
        notes="Same as KXBRENTD for weekly Brent strikes",
    ),
    "KXNATGASD": HedgeSpec(
        instrument="MNG", hedge_venue="CME",
        contract_size_units=2500.0, contract_unit="MMBtu",
        sigma_annual_default=0.55,
        notes="Micro NatGas, 2500 MMBtu/contract",
    ),
    "KXNATGASW": HedgeSpec(
        instrument="MNG", hedge_venue="CME",
        contract_size_units=2500.0, contract_unit="MMBtu",
        sigma_annual_default=0.55,
    ),
    "KXHOILW":   HedgeSpec(
        instrument="HO", hedge_venue="CME",
        contract_size_units=42000.0, contract_unit="gal",
        sigma_annual_default=0.40,
        notes="Heating Oil full contract (no micro); 42000 gal",
    ),

    # ── Metals ──────────────────────────────────────────────────────────
    "KXGOLDD":   HedgeSpec(
        instrument="MGC", hedge_venue="CME",
        contract_size_units=10.0, contract_unit="troy oz",
        sigma_annual_default=0.18,
        notes="Micro Gold, 10 oz/contract",
    ),
    "KXGOLDW":   HedgeSpec(
        instrument="MGC", hedge_venue="CME",
        contract_size_units=10.0, contract_unit="troy oz",
        sigma_annual_default=0.18,
    ),
    "KXSILVERD": HedgeSpec(
        instrument="SIL", hedge_venue="CME",
        contract_size_units=1000.0, contract_unit="troy oz",
        sigma_annual_default=0.32,
        notes="Micro Silver, 1000 oz/contract",
    ),
    "KXCOPPERD": HedgeSpec(
        instrument="MHG", hedge_venue="CME",
        contract_size_units=2500.0, contract_unit="lb",
        sigma_annual_default=0.30,
        notes="Micro Copper, 2500 lb/contract",
    ),

    # ── Grains (CME full contracts; no micros yet) ──────────────────────
    "KXCORNW":     HedgeSpec(
        instrument="ZC", hedge_venue="CME",
        contract_size_units=5000.0, contract_unit="bu",
        sigma_annual_default=0.25,
        notes="Corn full contract, 5000 bu; price in cents/bu",
    ),
    "KXWHEATW":    HedgeSpec(
        instrument="ZW", hedge_venue="CME",
        contract_size_units=5000.0, contract_unit="bu",
        sigma_annual_default=0.28,
    ),
    "KXSOYBEANW":  HedgeSpec(
        instrument="ZS", hedge_venue="CME",
        contract_size_units=5000.0, contract_unit="bu",
        sigma_annual_default=0.24,
    ),

    # ── Macro / rates (CME) ─────────────────────────────────────────────
    "KXCPIYOY":  HedgeSpec(
        instrument="ZQ", hedge_venue="CME",
        contract_size_units=4167000.0, contract_unit="DV01-$ proxy",
        sigma_annual_default=0.02,
        notes=("ZQ Fed Funds, $4167/01bp; CPI→Fed-funds mapping is "
               "approximate. Use small notional + monitor basis."),
    ),
    "KXFEDFUNDS": HedgeSpec(
        instrument="ZQ", hedge_venue="CME",
        contract_size_units=4167000.0, contract_unit="DV01-$ proxy",
        sigma_annual_default=0.02,
    ),

    # ── Equity index (CME micros) ───────────────────────────────────────
    "KXSPX":     HedgeSpec(
        instrument="MES", hedge_venue="CME",
        contract_size_units=5.0, contract_unit="$5/index pt",
        sigma_annual_default=0.18,
        notes="Micro E-mini S&P 500, $5 per index point",
    ),
    "KXSPY":     HedgeSpec(
        instrument="MES", hedge_venue="CME",
        contract_size_units=5.0, contract_unit="$5/index pt",
        sigma_annual_default=0.18,
    ),
    "KXNDQ":     HedgeSpec(
        instrument="MNQ", hedge_venue="CME",
        contract_size_units=2.0, contract_unit="$2/index pt",
        sigma_annual_default=0.25,
    ),

    # ── FX (CME micros) ─────────────────────────────────────────────────
    "KXEUR":     HedgeSpec(
        instrument="M6E", hedge_venue="CME",
        contract_size_units=12500.0, contract_unit="EUR",
        sigma_annual_default=0.09,
        notes="Micro EUR/USD, €12,500 notional",
    ),

    # ── Crypto (Kraken Pro perps) ───────────────────────────────────────
    "KXBTCMINMON": HedgeSpec(
        instrument="XBTUSD", hedge_venue="Kraken",
        contract_size_units=1.0, contract_unit="BTC",
        sigma_annual_default=0.65,
        notes="Kraken Pro perp; size in BTC. Funding rate not modelled.",
    ),
    "KXBTCMAXMON": HedgeSpec(
        instrument="XBTUSD", hedge_venue="Kraken",
        contract_size_units=1.0, contract_unit="BTC",
        sigma_annual_default=0.65,
    ),
    "KXETHMINMON": HedgeSpec(
        instrument="ETHUSD", hedge_venue="Kraken",
        contract_size_units=1.0, contract_unit="ETH",
        sigma_annual_default=0.75,
    ),
    "KXETHMAXMON": HedgeSpec(
        instrument="ETHUSD", hedge_venue="Kraken",
        contract_size_units=1.0, contract_unit="ETH",
        sigma_annual_default=0.75,
    ),

    # Phase E (2026-05-16): expand to cover currently-active crypto LIP
    # series. Each pair below maps to a Kraken Pro spot/perp pair when
    # available; HYPE has no Kraken listing, so hedge_venue is tagged
    # 'Hyperliquid' for now (execution path is dry-run-only until we
    # build a Hyperliquid adapter; the hedger still computes intent so
    # we measure basis residual against CoinGecko spot).
    "KXXRPMINMON": HedgeSpec(
        instrument="XRPUSD", hedge_venue="Kraken",
        contract_size_units=1.0, contract_unit="XRP",
        sigma_annual_default=0.85,
    ),
    "KXXRPMAXMON": HedgeSpec(
        instrument="XRPUSD", hedge_venue="Kraken",
        contract_size_units=1.0, contract_unit="XRP",
        sigma_annual_default=0.85,
    ),
    "KXSOLMINMON": HedgeSpec(
        instrument="SOLUSD", hedge_venue="Kraken",
        contract_size_units=1.0, contract_unit="SOL",
        sigma_annual_default=0.95,
    ),
    "KXSOLMAXMON": HedgeSpec(
        instrument="SOLUSD", hedge_venue="Kraken",
        contract_size_units=1.0, contract_unit="SOL",
        sigma_annual_default=0.95,
    ),
    "KXDOGEMINMON": HedgeSpec(
        instrument="XDGUSD", hedge_venue="Kraken",
        contract_size_units=1.0, contract_unit="DOGE",
        sigma_annual_default=1.20,
        notes="Kraken DOGE uses 'XDGUSD' symbol",
    ),
    "KXDOGEMAXMON": HedgeSpec(
        instrument="XDGUSD", hedge_venue="Kraken",
        contract_size_units=1.0, contract_unit="DOGE",
        sigma_annual_default=1.20,
    ),
    "KXADAMINMON": HedgeSpec(
        instrument="ADAUSD", hedge_venue="Kraken",
        contract_size_units=1.0, contract_unit="ADA",
        sigma_annual_default=0.85,
    ),
    "KXADAMAXMON": HedgeSpec(
        instrument="ADAUSD", hedge_venue="Kraken",
        contract_size_units=1.0, contract_unit="ADA",
        sigma_annual_default=0.85,
    ),
    "KXZECMINMON": HedgeSpec(
        instrument="ZECUSD", hedge_venue="Kraken",
        contract_size_units=1.0, contract_unit="ZEC",
        sigma_annual_default=1.10,
    ),
    "KXZECMAXMON": HedgeSpec(
        instrument="ZECUSD", hedge_venue="Kraken",
        contract_size_units=1.0, contract_unit="ZEC",
        sigma_annual_default=1.10,
    ),
    # Hyperliquid token — not on Kraken. Mark venue Hyperliquid; execution
    # path is dry-run until a HL adapter exists. Spot still fetched (HL has
    # a public API) so basis residual is measurable.
    "KXHYPEMINMON": HedgeSpec(
        instrument="HYPE-USD", hedge_venue="Hyperliquid",
        contract_size_units=1.0, contract_unit="HYPE",
        sigma_annual_default=1.30,
        notes="Hyperliquid native token; execution requires HL adapter.",
    ),
    "KXHYPEMAXMON": HedgeSpec(
        instrument="HYPE-USD", hedge_venue="Hyperliquid",
        contract_size_units=1.0, contract_unit="HYPE",
        sigma_annual_default=1.30,
    ),

    # ── Polymarket US — same-event hedge (1:1 contract count) ────────────
    # Phase F (2026-05-18): use PM as the hedge venue for series with no
    # continuous-underlying counterpart. PM US (CFTC DCM, Feb 2026 launch)
    # focuses on political + macro + major news; sports/entertainment/
    # rich-crypto-ladder are NOT covered — those still rely on Kraken/CME
    # or stay unhedged. Each entry's `instrument` is a PLACEHOLDER PM
    # slug; the live slug resolves per-fill via
    # cross_venue.kalshi_pm_map.find_pm_counterpart(ticker).
    #
    # When Kalshi YES fills: PM adapter places NO of same event (1:1).
    # When Kalshi NO fills:  PM adapter places YES of same event (1:1).
    # Combined position pays $1 guaranteed regardless of outcome.
    #
    # hedge_strategy=same_event_unit bypasses delta math: qty = -kalshi_contracts.
    #
    # ONLY enable series where:
    #   1. Kalshi and PM list materially the SAME event (not just adjacent)
    #   2. Outcome semantics are 1:1 (Kalshi YES ↔ PM YES of same event)
    #   3. Both venues have liquid books at our quote size
    "KXVOTEHUBTRUMPUPDOWN": HedgeSpec(
        instrument="trump-approval-week",   # placeholder; resolved per-ticker
        hedge_venue="Polymarket",
        contract_size_units=1.0, contract_unit="contracts",
        hedge_strategy="same_event_unit",
        notes="Trump approval rating; PM has weekly equivalents.",
    ),
    "KXMAMDANIEO": HedgeSpec(
        instrument="mamdani-executive-order",
        hedge_venue="Polymarket",
        contract_size_units=1.0, contract_unit="contracts",
        hedge_strategy="same_event_unit",
        notes="NYC mayoral EO; PM lists Mamdani actions.",
    ),
    "KXTRUMPENDORSEMENTS": HedgeSpec(
        instrument="trump-endorses-candidate-week",
        hedge_venue="Polymarket",
        contract_size_units=1.0, contract_unit="contracts",
        hedge_strategy="same_event_unit",
        notes="Trump weekly endorsement count; PM has parallel markets.",
    ),
    "KXLAKECONF": HedgeSpec(
        instrument="kari-lake-confirmation",
        hedge_venue="Polymarket",
        contract_size_units=1.0, contract_unit="contracts",
        hedge_strategy="same_event_unit",
        notes="Kari Lake ambassador confirmation date; PM equivalent.",
    ),
    "KXCPIYOY_PM": HedgeSpec(
        # CPI is the rare case where BOTH PM US and CME (via IBKR ZQ) are
        # viable hedges. PM US lists CPI YoY ranges natively. CME ZQ gives
        # Fed-rate-implied CPI sensitivity. Use PM until IBKR live, then
        # add basis comparison. Key suffixed _PM so the CME entry remains
        # primary in the lookup once that adapter is funded.
        instrument="cpi-yoy-month",
        hedge_venue="Polymarket",
        contract_size_units=1.0, contract_unit="contracts",
        hedge_strategy="same_event_unit",
        notes="CPI YoY range; PM US lists these natively (DCM-approved).",
    ),
    # NOTE: KXBTC* / KXETH* etc. NOT routed to Polymarket because PM US
    # doesn't list the same daily/monthly strike ladder Kalshi has. Crypto
    # stays on Kraken (already wired with full delta-hedge math).
}


# Index series prefixes whose markets are typically multi-outcome /
# event-driven and do NOT have a viable continuous hedge. Listed
# explicitly so the hedger can mark them as `manual_hedge_only` or
# `no_hedge` instead of mis-applying a numerical hedge ratio.
NO_HEDGE_SERIES: set[str] = {
    # Sports & entertainment — alpha venues only (DraftKings, etc.)
    "KXNBAMENTION", "KXNBACOACH", "KXMLBMANAGEROUT",
    # Politics / govt actions
    "KXPRES", "KXPREZ", "KXELEC", "KXMIDTERM", "KXEOWEEK",
    # Pop culture
    "KXMETGALA", "KXKIMMELAPOLOGY", "KXBILLSCOUNT",
}


# ── Lookup helpers ──────────────────────────────────────────────────────────

_TICKER_SERIES_RE = re.compile(r"^([A-Z0-9]+?)(?:-|$)")


def series_prefix(ticker: str) -> str:
    """Extract the series prefix from a Kalshi ticker.

    Examples:
        KXBRENTD-26JUN0117-T100 → KXBRENTD
        KXCPIYOY-2604-T2.5      → KXCPIYOY
    """
    m = _TICKER_SERIES_RE.match(ticker)
    return m.group(1) if m else ticker


def hedge_for_ticker(ticker: str) -> Optional[HedgeSpec]:
    """Return the HedgeSpec for a ticker, or None if not hedge-eligible."""
    return HEDGE_MAP.get(series_prefix(ticker))


def is_hedge_eligible(ticker_or_series: str) -> bool:
    """True if the ticker / series has a continuous-hedge mapping."""
    prefix = series_prefix(ticker_or_series)
    return prefix in HEDGE_MAP


def is_explicit_no_hedge(ticker_or_series: str) -> bool:
    """True if the series is explicitly tagged no-hedge (sports, politics)."""
    return series_prefix(ticker_or_series) in NO_HEDGE_SERIES


def hedge_venues_in_use() -> set[str]:
    """Distinct external venues referenced by HEDGE_MAP. Used by the hedger
    to decide which adapters to import / connect."""
    return {spec.hedge_venue for spec in HEDGE_MAP.values()}


# ── Self-tests ─────────────────────────────────────────────────────────────

def _self_test() -> None:
    """Smoke checks for the mapping + delta math."""
    # Ticker parsing
    assert series_prefix("KXBRENTD-26JUN0117-T100") == "KXBRENTD"
    assert series_prefix("KXCPIYOY-2604-T2.5") == "KXCPIYOY"
    assert series_prefix("STANDALONE") == "STANDALONE"

    # Mapping lookup
    assert hedge_for_ticker("KXBRENTD-26JUN0117-T100") is not None
    assert hedge_for_ticker("KXBRENTD-26JUN0117-T100").instrument == "BZ"
    assert hedge_for_ticker("KXBTCMINMON-26MAY-T100000").instrument == "XBTUSD"
    assert hedge_for_ticker("KXNFL-foo") is None

    # No-hedge tagging
    assert is_explicit_no_hedge("KXPRES-2028")
    assert not is_explicit_no_hedge("KXBRENTD-26JUN")

    # Binary delta — sanity:
    #   spot below strike, near-expiry, low vol → small positive delta
    #   spot at strike, near-expiry → peaks
    d_otm = binary_delta(spot=90.0, strike=100.0, sigma_annual=0.30, years_to_settle=1/365)
    d_atm = binary_delta(spot=100.0, strike=100.0, sigma_annual=0.30, years_to_settle=1/365)
    d_itm = binary_delta(spot=110.0, strike=100.0, sigma_annual=0.30, years_to_settle=1/365)
    assert d_atm > d_otm and d_atm > d_itm, (
        f"binary call delta should peak at-the-money: otm={d_otm}, atm={d_atm}, itm={d_itm}"
    )

    # Hedge sizing example: 200 Kalshi YES contracts on KXBRENTD T100,
    # spot 98.50, 12h to settle. Should return a small NEGATIVE qty
    # (short BZ to hedge the synthetic long-call exposure).
    spec = hedge_for_ticker("KXBRENTD-26JUN0117-T100")
    qty = spec.hedge_qty(
        kalshi_contracts=200, strike_price=100.0,
        spot=98.50, hours_to_settle=12,
    )
    assert qty < 0, f"long YES Kalshi → short futures expected, got qty={qty}"
    # Magnitude should be tiny — 200 binary contracts × $1 × δ / 1000 bbl ≈ <0.1
    assert abs(qty) < 1.0, f"BZ qty unreasonably large: {qty}"

    # Hedge_venues_in_use
    venues = hedge_venues_in_use()
    assert "CME" in venues and "ICE" in venues and "Kraken" in venues

    print("market_match self-test: PASS "
          f"(mapped series: {len(HEDGE_MAP)}, venues: {sorted(venues)})")


if __name__ == "__main__":
    _self_test()
