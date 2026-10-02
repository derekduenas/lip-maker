"""FairValueCache drops per-market metadata (titles, strike hints, public
market fetches) for markets past close + 1 day, and for markets no longer
asked for in a week when their close is unknown."""
import time
from datetime import datetime, timezone

import pytest

from mm.unattended import fairvalue as F
from tests.test_fv_quote import MKT, _Http, _ens_payload, _env  # noqa: F401


def _cache():
    c = F.FairValueCache(session=_Http(_ens_payload()), sleep=lambda s: None)
    return c


def test_prune_past_close_plus_a_day_and_long_untargeted():
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp()
    c = _cache()
    old_kx = "KXHIGHNY-26OCT01-B72.5"              # window ended 2026-10-02 05:00Z
    new_kx = "KXHIGHNY-26OCT06-B72.5"
    known = "KXFED-26DEC-T4.00"
    unknown = "KXSOMETHING-XYZ"
    for m in (old_kx, new_kx, known, unknown):
        c.titles[m] = "t"
        c.hints[m] = {"strike_type": "between"}
        c.market_meta[m] = {"title": "t"}
    c.market_meta[known]["close_ts"] = now - 2 * 86400.0
    c._prune_meta([old_kx, new_kx, unknown], now - 8 * 86400.0)      # all targeted a week ago
    c._prune_meta([new_kx], now)
    for d in (c.titles, c.hints, c.market_meta):
        assert old_kx not in d and known not in d and unknown not in d
        assert new_kx in d
    # a market still asked for is kept even without a close
    c.titles["KXFOO-1"] = "t"
    c._prune_meta(["KXFOO-1"], now + 30 * 86400.0)
    assert "KXFOO-1" in c.titles


def test_public_fetch_records_close_and_refresh_prunes(monkeypatch):
    c = _cache()
    c.http = _Closed()
    c._kalshi_title("KXFED-26DEC-T4.00")
    assert c.market_meta["KXFED-26DEC-T4.00"]["close_ts"] == pytest.approx(
        datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    monkeypatch.setenv("LIP_FV_PM_PAGES", "0")
    monkeypatch.setenv("LIP_FV_CALIB_ENABLE", "0")
    c.refresh([])
    assert "KXFED-26DEC-T4.00" not in c.titles and "KXFED-26DEC-T4.00" not in c.market_meta


class _Closed:
    def get(self, url, params=None, headers=None, timeout=None):
        class R:
            status_code = 200

            def json(self_inner):
                return {"market": {"title": "Fed", "close_time": "2026-01-01T00:00:00Z"}}
        return R()
