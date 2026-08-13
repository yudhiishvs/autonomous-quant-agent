"""Safety-critical constants shared by the forward paper-trading subsystem."""

from __future__ import annotations

from zoneinfo import ZoneInfo

PROJECT_ORDER_PREFIX = "apa"
MAX_CLIENT_ORDER_ID_LENGTH = 48
DATABASE_SCHEMA_VERSION = 1

PAPER_API_KEY_ENV = "APA_ALPACA_PAPER_API_KEY"
PAPER_SECRET_KEY_ENV = "APA_ALPACA_PAPER_SECRET_KEY"
PAPER_ORDER_ENABLEMENT_ENV = "APA_ENABLE_PAPER_ORDERS"
PAPER_ORDER_ACKNOWLEDGEMENT = "I_ACKNOWLEDGE_PAPER_ONLY"
PAPER_RESUME_ACKNOWLEDGEMENT = "I_HAVE_REVIEWED_THE_PAPER_ACCOUNT"
PAPER_FLATTEN_ACKNOWLEDGEMENT = "I_ACKNOWLEDGE_SIMULATED_PAPER_LIQUIDATION"

PAPER_TRADING_BANNER = "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS"
IEX_FEED_BANNER = "REAL-TIME IEX FEED — NOT THE FULL CONSOLIDATED US MARKET"
SIP_FEED_BANNER = "REAL-TIME SIP FEED"

UTC = ZoneInfo("UTC")
NEW_YORK = ZoneInfo("America/New_York")

# OTC and crypto venues are intentionally absent.  Alpaca represents stocks and
# ETFs with the same US_EQUITY asset class, so an exchange allowlist is the
# additional structural check that keeps the proof of concept US-listed only.
SUPPORTED_US_EQUITY_EXCHANGES = frozenset(
    {"AMEX", "ARCA", "ASCX", "BATS", "NYSE", "NASDAQ", "NYSEARCA"}
)

SUPPORTED_DATA_FEEDS = frozenset({"IEX", "SIP"})
NORMAL_ORDER_MODES = frozenset({"paper_once", "paper_run"})


def feed_banner(feed: str) -> str:
    """Return the required user-facing disclosure for an explicitly selected feed."""

    normalized = str(feed).strip().upper()
    if normalized == "IEX":
        return IEX_FEED_BANNER
    if normalized == "SIP":
        return SIP_FEED_BANNER
    raise ValueError(f"Unsupported market-data feed: {feed!r}")
