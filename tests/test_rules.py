"""Tests for engine/rules.py — extract_trigger_phrase parsing.

Live-validated 2026-05-09: pulled 14 live Kalshi mention markets via
authenticated client, manually scored extract_trigger_phrase() output
against subtitle ground truth.  Result: 14/14 match (0% mismatch),
well below audit gate of 15% mismatch tolerance.

Caveat: all 14 sampled markets are from same series (KXEARNINGSMENTIONHIMS,
Hims & Hers May 11 2026 earnings) — single rule template.  To extend
coverage, re-run /tmp/audit_rules.py during the next earnings cycle
when more issuers have open mention markets, then add additional cases.
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from engine.rules import extract_trigger_phrase


# ── Live-validated cases (2026-05-09) ─────────────────────────────────
LIVE_CASES = [
    # (rule_text, expected_trigger_phrase, expected_includes_qa)
    (
        'If Cancer is said by any Hims & Hers Health, Inc. representative '
        '(including the operator of the call) during the next Hims & Hers '
        'Health, Inc. earnings call (including the Q+A), then the market '
        'resolves to Yes.',
        'cancer', True,
    ),
    (
        'If Tariff is said by any Hims & Hers Health, Inc. representative '
        '(including the operator of the call) during the next Hims & Hers '
        'Health, Inc. earnings call (including the Q+A), then the market '
        'resolves to Yes.',
        'tariff', True,
    ),
    (
        'If Microdose / Microdosing is said by any Hims & Hers Health, Inc. '
        'representative (including the operator of the call) during the next '
        'Hims & Hers Health, Inc. earnings call (including the Q+A), then '
        'the market resolves to Yes.',
        'microdose / microdosing', True,
    ),
    (
        'If Lab Testing is said by any Hims & Hers Health, Inc. '
        'representative (including the operator of the call) during the '
        'next Hims & Hers Health, Inc. earnings call (including the Q+A), '
        'then the market resolves to Yes.',
        'lab testing', True,
    ),
    (
        'If FDA is said by any Hims & Hers Health, Inc. representative '
        '(including the operator of the call) during the next Hims & Hers '
        'Health, Inc. earnings call (including the Q+A), then the market '
        'resolves to Yes.',
        'fda', True,
    ),
]


@pytest.mark.parametrize("rule,expected_trigger,expected_qa", LIVE_CASES)
def test_extract_trigger_phrase_live(rule, expected_trigger, expected_qa):
    """Each live-pulled rule parses to the expected trigger + Q&A flag."""
    out = extract_trigger_phrase(rule)
    assert out["trigger_phrase"] == expected_trigger, \
        f"got {out['trigger_phrase']!r}, expected {expected_trigger!r}"
    assert out["includes_qa"] == expected_qa


def test_variant_rule_splits_on_slash():
    """Slash-separated variants ('X / Y') should produce both."""
    rule = ('If Microdose / Microdosing is said by any Hims & Hers Health, Inc. '
            'representative during the next earnings call.')
    out = extract_trigger_phrase(rule)
    assert "microdose" in out["variants"]
    assert "microdosing" in out["variants"]
    assert out["exact_match"] is False  # multi-variant


def test_empty_input_returns_safe_default():
    out = extract_trigger_phrase("")
    assert out["trigger_phrase"] is None
    assert out["variants"] == []


def test_unquoted_trigger_pattern():
    """Catches 'If X is said' (no quotes around X)."""
    rule = "If Recession is mentioned by any Apple representative."
    out = extract_trigger_phrase(rule)
    assert out["trigger_phrase"] is not None
    assert "recession" in (out["trigger_phrase"] or "")


def test_scope_detects_operator_and_qa():
    rule = ('If "Inflation" is said by any Apple representative '
            '(including the operator) during the call (including Q+A).')
    out = extract_trigger_phrase(rule)
    assert "operator" in out["scope"]
    assert out["includes_qa"] is True
