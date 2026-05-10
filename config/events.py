"""Known event dates — FOMC + Earnings + Political events calendar."""

from datetime import datetime, timedelta

# Historical FOMC press conference dates (Powell era: 2018-present)
FOMC_PRESSER_DATES = [
    "2018-03-21", "2018-06-13", "2018-09-26", "2018-12-19",
    "2019-01-30", "2019-03-20", "2019-05-01", "2019-06-19",
    "2019-07-31", "2019-09-18", "2019-10-30", "2019-12-11",
    "2020-01-29", "2020-03-03", "2020-03-15", "2020-04-29",
    "2020-06-10", "2020-07-29", "2020-09-16", "2020-11-05", "2020-12-16",
    "2021-01-27", "2021-03-17", "2021-04-28", "2021-06-16",
    "2021-07-28", "2021-09-22", "2021-11-03", "2021-12-15",
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15",
    "2022-07-27", "2022-09-21", "2022-11-02", "2022-12-14",
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14",
    "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12",
    "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-11-05", "2025-12-17",
    "2026-01-28", "2026-03-18", "2026-05-07", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-11-04", "2026-12-16",
]

# Shared term lists for events
FOMC_TERMS_LIST = [
    "inflation", "disinflation", "recession", "stagflation", "soft landing",
    "labor market", "unemployment", "rate cut", "rate hike", "pause",
    "neutral rate", "balance sheet", "quantitative tightening", "QT",
    "tariffs", "trade", "uncertainty", "confident", "gradual", "data dependent",
    "housing", "credit", "financial conditions", "headwinds", "resilient",
    "transitory", "pivot", "restrictive", "accommodation",
]

TSLA_TERMS = [
    "autonomy", "FSD", "full self-driving", "Dojo", "Optimus", "cybertruck",
    "demand", "deliveries", "production", "margins", "tariffs", "China",
    "AI", "robotaxi", "supercharger", "gigafactory", "battery", "semi",
    "energy storage", "solar", "headwinds", "guidance",
]

# UPCOMING EVENTS — next 90 days (scan 24h before each)
UPCOMING_EVENTS = [
    # --- Week of Apr 20 (Earnings wave 1) ---
    {
        "event_type": "tsla_earnings",
        "event_date": "2026-04-22",
        "speaker": "musk",
        "kalshi_category": "mentions",
        "terms": TSLA_TERMS,
        "scan_at": "2026-04-21T10:00:00",
    },
    {
        "event_type": "intc_earnings",
        "event_date": "2026-04-23",
        "speaker": "lip_bu",
        "kalshi_category": "mentions",
        "terms": None,  # populated when corpus is built
        "scan_at": "2026-04-22T10:00:00",
    },
    {
        "event_type": "ba_earnings",
        "event_date": "2026-04-23",
        "speaker": "ortberg",
        "kalshi_category": "mentions",
        "terms": None,
        "scan_at": "2026-04-22T10:00:00",
    },
    # --- Week of Apr 27 (Earnings wave 2 — big tech) ---
    {
        "event_type": "msft_earnings",
        "event_date": "2026-04-28",
        "speaker": "nadella",
        "kalshi_category": "mentions",
        "terms": None,
        "scan_at": "2026-04-27T10:00:00",
    },
    {
        "event_type": "meta_earnings",
        "event_date": "2026-04-29",
        "speaker": "zuckerberg",
        "kalshi_category": "mentions",
        "terms": None,
        "scan_at": "2026-04-28T10:00:00",
    },
    {
        "event_type": "aapl_earnings",
        "event_date": "2026-04-30",
        "speaker": "cook",
        "kalshi_category": "mentions",
        "terms": None,
        "scan_at": "2026-04-29T10:00:00",
    },
    {
        "event_type": "amzn_earnings",
        "event_date": "2026-04-30",
        "speaker": "jassy",
        "kalshi_category": "mentions",
        "terms": None,
        "scan_at": "2026-04-29T10:00:00",
    },
    # --- FOMC (next 90 days) ---
    {
        "event_type": "fomc_presser",
        "event_date": "2026-05-07",
        "speaker": "powell",
        "kalshi_category": "mentions",
        "terms": FOMC_TERMS_LIST,
        "scan_at": "2026-05-06T10:00:00",
    },
    # --- May/June earnings ---
    # 2026-05-10: HIMS earnings 24h out — markets verified live on Kalshi
    # (KXEARNINGSMENTIONHIMS-26MAY11-* in active status). Speaker key "hims"
    # has zero corpus → thin-corpus gate should REJECT all HIMS edges.
    {
        "event_type": "hims_earnings",
        "event_date": "2026-05-11",
        "speaker": "hims",
        "kalshi_category": "mentions",
        "terms": None,
        "scan_at": "2026-05-11T10:00:00",
    },
    # 2026-05-10 (corrected): NVDA Q1 FY2027 — Wed May 20, 2026, 2pm PT call
    # = 5pm ET = 21:00 UTC. Verified via NVIDIA IR + multiple sources.
    # (Earlier 5/21 was operator estimate; real call is 5/20.)
    # Speaker "huang" corpus n=8 ✓.
    {
        "event_type": "nvda_earnings",
        "event_date": "2026-05-20",
        "call_time":  "2026-05-20T21:00:00",
        "speaker": "huang",
        "kalshi_category": "mentions",
        "terms": None,
        "scan_at": "2026-05-19T21:00:00",
    },
    # 2026-05-10: WMT FY2027 Q1 — Thu May 21, 2026, 7am CT release = 13:00 UTC.
    # Verified via Walmart IR. Speaker "mcmillon" corpus n=8 ✓.
    # Kalshi /events shows KXEARNINGSMENTIONWMT-26JUN30 cycle (markets
    # initialize closer to call).
    {
        "event_type": "wmt_earnings",
        "event_date": "2026-05-21",
        "call_time":  "2026-05-21T13:00:00",
        "speaker": "mcmillon",
        "kalshi_category": "mentions",
        "terms": None,
        "scan_at": "2026-05-20T13:00:00",
    },
    {
        "event_type": "fomc_presser",
        "event_date": "2026-06-17",
        "speaker": "powell",
        "kalshi_category": "mentions",
        "terms": FOMC_TERMS_LIST,
        "scan_at": "2026-06-16T10:00:00",
    },
    {
        "event_type": "fomc_presser",
        "event_date": "2026-07-29",
        "speaker": "powell",
        "kalshi_category": "mentions",
        "terms": FOMC_TERMS_LIST,
        "scan_at": "2026-07-28T10:00:00",
    },
]


def get_upcoming_events(within_hours: int = 48) -> list:
    """Returns events occurring within the next `within_hours`."""
    now = datetime.utcnow()
    cutoff = now + timedelta(hours=within_hours)
    results = []
    for event in UPCOMING_EVENTS:
        event_dt = datetime.strptime(event["event_date"], "%Y-%m-%d")
        if now <= event_dt <= cutoff:
            results.append(event)
    return results


def get_events_needing_scan(within_hours: int = 48) -> list:
    """Returns events whose scan_at time is within the next `within_hours`."""
    now = datetime.utcnow()
    cutoff = now + timedelta(hours=within_hours)
    results = []
    for event in UPCOMING_EVENTS:
        scan_dt = datetime.strptime(event["scan_at"], "%Y-%m-%dT%H:%M:%S")
        if now <= scan_dt <= cutoff:
            results.append(event)
    return results


def get_recently_resolved(within_hours: int = 48) -> list:
    """Returns events that occurred in the last `within_hours` (need review)."""
    now = datetime.utcnow()
    lookback = now - timedelta(hours=within_hours)
    results = []
    for event in UPCOMING_EVENTS:
        event_dt = datetime.strptime(event["event_date"], "%Y-%m-%d")
        if lookback <= event_dt <= now:
            results.append(event)
    return results
