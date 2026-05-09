"""Multi-Engine Configuration — each engine has its own corpus, thresholds, and allocation."""

ENGINES = {
    "fed_ecosystem": {
        "event_types": [
            "fomc_presser", "fomc_minutes", "fomc_statement",
            "fed_testimony", "fed_speech_jackson_hole",
            "fed_speech_general", "fed_governor_speech",
        ],
        "min_edge": 0.10,
        "kelly_multiplier": 1.0,
        "capital_allocation": 0.25,
        "max_concurrent": 8,
        "corpus_min_n": 6,
        "status": "ACTIVE",
        "description": "Fed ecosystem — FOMC + testimony + speeches + minutes",
    },
    "political_daily": {
        "event_types": [
            "white_house_briefing", "trump_speech",
            "trump_social", "political_press_conf",
            "rfk_speech", "political_speech",
        ],
        "min_edge": 0.15,
        "kelly_multiplier": 0.5,
        "capital_allocation": 0.30,
        "max_concurrent": 10,
        "corpus_min_n": 8,
        "status": "PENDING_BUILD",  # DISABLED 2026-04-19: walkforward 2W/3L, paper 0W/1L. Re-enable after edge validated.
        "description": "Political daily — WH briefings + Trump + political events",
    },
    "nfl_broadcast": {
        "event_types": [
            "nfl_game_broadcast", "nfl_broadcast",
            "nfl_commentator_mention",
        ],
        "min_edge": 0.12,
        "kelly_multiplier": 0.7,
        "capital_allocation": 0.25,
        "max_concurrent": 15,
        "corpus_min_n": 10,
        "status": "PENDING_BUILD",  # Session 13 — pre-NFL season
        "seasonal": ["september", "october", "november",
                     "december", "january", "february"],
        "description": "NFL broadcast mentions — in-season only",
    },
    "corporate_calendar": {
        "event_types": [
            "tsla_earnings", "nflx_earnings", "nvda_earnings",
            "aapl_earnings", "meta_earnings", "amzn_earnings",
            "msft_earnings", "intc_earnings", "ba_earnings",
            # Wildcard for auto-discovered tickers
        ],
        "min_edge": 0.20,
        "kelly_multiplier": 0.5,
        "capital_allocation": 0.20,
        "max_concurrent": 8,
        "corpus_min_n": 10,
        "status": "SECONDARY",  # only trade when n>=10
        "description": "Corporate earnings + events — high threshold",
    },
}


def get_engine_for_event(event_type: str) -> dict | None:
    """Find which engine handles this event type."""
    for name, engine in ENGINES.items():
        if engine["status"] in ("PENDING_BUILD",):
            continue
        for et in engine["event_types"]:
            if et == event_type:
                return {**engine, "engine_name": name}
            # Wildcard match for earnings
            if et.endswith("_earnings") and event_type.endswith("_earnings"):
                if et.startswith(event_type.split("_")[0]):
                    return {**engine, "engine_name": name}
        # Check wildcard patterns
        if any(event_type.endswith(suffix) for et in engine["event_types"]
               if et.startswith("*") for suffix in [et[1:]]):
            return {**engine, "engine_name": name}
    return None


def get_active_engines() -> dict:
    """Return only engines with status ACTIVE or SECONDARY."""
    return {name: eng for name, eng in ENGINES.items()
            if eng["status"] in ("ACTIVE", "SECONDARY")}
