"""K5: assert top_n_to_quote fails closed (returns []) on SQL error
instead of silently falling back to a permissive query that quotes settled
or blacklisted markets.
"""
import sqlite3
from unittest.mock import patch
import pytest


def test_top_n_to_quote_fails_closed_on_sql_error(tmp_path):
    """When the primary SQL raises OperationalError, return [] + emit alert.
    Previously fell back to a query missing end_date + blacklist filters.
    """
    from engine import lip_discovery

    db = tmp_path / "fail.db"
    # Create empty DB so connect() succeeds but the table doesn't exist
    sqlite3.connect(str(db)).close()

    alert_calls = []

    def fake_alert(level, source, message):
        alert_calls.append((level, source, message))

    with patch("monitor.alerts.alert", fake_alert):
        result = lip_discovery.top_n_to_quote(n=10, db_path=str(db))

    # Empty list returned (fail-closed)
    assert result == [], f"expected empty list on SQL error, got {len(result)} items"
    # Alert was emitted
    assert len(alert_calls) == 1, f"expected 1 alert, got {len(alert_calls)}"
    level, source, msg = alert_calls[0]
    assert level == "ERROR"
    assert source == "lip_discovery"
    assert "SQL failed" in msg
    assert "EMPTY list" in msg
