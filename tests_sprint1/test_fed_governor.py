"""Tests for prep/fed_governor_corpus.py — speaker parsing, body extraction."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prep.fed_governor_corpus import extract_speaker


def test_extract_speaker_modern_format():
    """Primary Fed feed format: 'LastName, Title'."""
    assert extract_speaker("Waller, One Transitory Shock After Another") == "waller"
    assert extract_speaker("Powell, Outlook for the Economy") == "powell"
    assert extract_speaker("Jefferson, Labor Market Update") == "jefferson"


def test_extract_speaker_legacy_chair():
    assert extract_speaker("Speech by Chair Powell on inflation") == "powell"


def test_extract_speaker_legacy_governor():
    assert extract_speaker("Speech by Governor Waller on markets") == "waller"


def test_extract_speaker_legacy_vice_chair():
    assert extract_speaker("Speech by Vice Chair Jefferson on policy") == "jefferson"


def test_extract_speaker_no_title():
    """Fallback to generic 'fed' if no recognized pattern."""
    assert extract_speaker("Some title without any title keyword") == "fed"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
