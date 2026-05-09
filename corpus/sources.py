"""Canonical transcript source definitions with quality constraints."""

EARNINGS_SOURCES = [
    {
        "name": "motley_fool",
        "priority": 1,
        "quality_gates": {
            "min_word_count": 2000,
            "max_word_count": 50000,
            "required_phrases": [
                "thank you", "operator", "question", "quarter",
                "revenue", "earnings per share", "forward-looking"
            ],
            "forbidden_phrases": [
                "404", "page not found", "access denied",
                "subscribe to read", "premium content",
                "sign in to continue", "javascript required"
            ],
            "min_speaker_changes": 3,
        },
    },
    {
        "name": "seeking_alpha",
        "priority": 2,
        "quality_gates": {
            "min_word_count": 2000,
            "max_word_count": 50000,
            "required_phrases": [
                "thank you", "question", "quarter", "revenue"
            ],
            "forbidden_phrases": [
                "404", "page not found", "unlock this article",
                "become a premium member", "paywall"
            ],
            "min_speaker_changes": 3,
        },
    },
    {
        "name": "generic",
        "priority": 3,
        "quality_gates": {
            "min_word_count": 1000,
            "max_word_count": 100000,
            "required_phrases": ["quarter"],
            "forbidden_phrases": ["404", "page not found", "access denied"],
            "min_speaker_changes": 1,
        },
    },
]

TICKER_SPEAKER_MAP = {
    "TSLA": "musk", "NVDA": "huang", "AAPL": "cook", "META": "zuckerberg",
    "RDDT": "huffman", "CMG": "niccol", "NFLX": "peters", "AMZN": "jassy",
    "MSFT": "nadella", "GOOGL": "pichai", "JPM": "dimon", "GS": "solomon",
    "PEP": "laguarta", "KO": "quincey", "MCD": "kempczinski", "BA": "ortberg",
    "INTC": "lip_bu", "AMD": "su", "CRM": "benioff", "UBER": "khosrowshahi",
    "TFC": "rogers", "PGR": "griffith", "UAL": "kirby", "GEV": "strazik",
    "ALLY": "brown", "PG": "moeller",
    "FDX": "subramaniam",
    # Political speakers — not tickers but the scanner uses this map
    "TRUMP": "trump",
}


def get_speaker(ticker: str) -> str:
    return TICKER_SPEAKER_MAP.get(ticker.upper(), "unknown")


def get_event_type(ticker: str) -> str:
    return f"{ticker.lower()}_earnings"
