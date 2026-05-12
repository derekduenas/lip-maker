"""Pin WeatherBrain ticker parsing + probability math.

Particularly important: yes_sub_title direction parsing. A single ticker
like KXLOWTLAX-26MAY12-T60 can mean ">60°" OR "<60°" depending on the
yes_sub_title — if we get this wrong we systematically take the wrong
side on every weather trade.

Run: python -m unittest tests.test_weather_brain
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.brains.weather import (
    parse_market, p_yes_for, _stdev_for_lead,
    WeatherBrain, ALL_PREFIXES,
)


class TestTickerParsing(unittest.TestCase):
    def test_low_above(self):
        m = parse_market({
            "ticker": "KXLOWTLAX-26MAY12-T60",
            "yes_sub_title": "61° or above",
        })
        self.assertIsNotNone(m)
        self.assertEqual(m.city_token, "LAX")
        self.assertEqual(m.nws_station, "KLAX")
        self.assertFalse(m.is_high)
        self.assertEqual(m.direction, "above")
        self.assertEqual(m.yes_value, 61.0)
        self.assertEqual(m.strike_temp, 60.0)

    def test_low_below(self):
        m = parse_market({
            "ticker": "KXLOWTLAX-26MAY12-T53",
            "yes_sub_title": "52° or below",
        })
        self.assertIsNotNone(m)
        self.assertEqual(m.direction, "below")
        self.assertEqual(m.yes_value, 52.0)
        self.assertFalse(m.is_high)

    def test_high_above(self):
        m = parse_market({
            "ticker": "KXHIGHTPHX-26JUN15-T100",
            "yes_sub_title": "101° or above",
        })
        self.assertIsNotNone(m)
        self.assertTrue(m.is_high)
        self.assertEqual(m.nws_station, "KPHX")
        self.assertEqual(m.direction, "above")
        self.assertEqual(m.yes_value, 101.0)

    def test_filter_non_temp_inflation(self):
        # KXHIGHINFLATION shares the KXHIGH prefix but isn't temperature
        m = parse_market({
            "ticker": "KXHIGHINFLATION-26MAY-T3",
            "yes_sub_title": "3% or above",
        })
        self.assertIsNone(m)

    def test_filter_unknown_city(self):
        m = parse_market({
            "ticker": "KXHIGHTNOWHERE-26MAY12-T80",
            "yes_sub_title": "81° or above",
        })
        self.assertIsNone(m)

    def test_range_markets_skipped(self):
        # B prefix on the strike means "between" — not yet supported
        m = parse_market({
            "ticker": "KXLOWTAUS-26MAY12-B63.5",
            "yes_sub_title": "63° to 64°",
        })
        self.assertIsNone(m, "B-range tickers should be skipped (parser only handles T-threshold)")


class TestProbabilityMath(unittest.TestCase):
    def test_above_far(self):
        # forecast 70, threshold ≥60 with σ=2 → very high prob
        p = p_yes_for("above", forecast_temp=70, yes_value=60, sigma=2.0)
        self.assertGreater(p, 0.99)

    def test_above_far_below(self):
        p = p_yes_for("above", forecast_temp=50, yes_value=60, sigma=2.0)
        self.assertLess(p, 0.01)

    def test_below_symmetry(self):
        # Below the threshold by 5° with σ=2 → very high prob YES
        p = p_yes_for("below", forecast_temp=55, yes_value=60, sigma=2.0)
        self.assertGreater(p, 0.99)

    def test_at_threshold_about_half(self):
        # Forecast equals threshold → roughly 50-50 (after continuity correction
        # slightly biased toward the strict inequality)
        p = p_yes_for("above", forecast_temp=60, yes_value=60, sigma=2.0)
        self.assertGreater(p, 0.4)
        self.assertLess(p, 0.7)

    def test_clamped_in_range(self):
        # Extreme forecast → should still clamp to (0.001, 0.999)
        p_lo = p_yes_for("above", forecast_temp=0,  yes_value=100, sigma=2.0)
        p_hi = p_yes_for("above", forecast_temp=200, yes_value=100, sigma=2.0)
        self.assertGreaterEqual(p_lo, 0.001)
        self.assertLessEqual(p_hi, 0.999)


class TestStdevByLead(unittest.TestCase):
    def test_short_lead_tighter(self):
        # 0-day lead should be tighter than 7-day lead
        self.assertLess(_stdev_for_lead(0), _stdev_for_lead(7))

    def test_beyond_7d_widest(self):
        self.assertGreaterEqual(_stdev_for_lead(14), _stdev_for_lead(7))


class TestBrainCanEvaluate(unittest.TestCase):
    def test_all_prefixes_match(self):
        brain = WeatherBrain.__new__(WeatherBrain)
        for p in ALL_PREFIXES:
            self.assertTrue(WeatherBrain.can_evaluate(brain, f"{p}NY-26MAY12-T70"))

    def test_unrelated_ticker_rejected(self):
        brain = WeatherBrain.__new__(WeatherBrain)
        self.assertFalse(WeatherBrain.can_evaluate(brain, "KXRANKLISTSONGSPOTGLOBAL-26JUN01-NIC"))


if __name__ == "__main__":
    unittest.main()
