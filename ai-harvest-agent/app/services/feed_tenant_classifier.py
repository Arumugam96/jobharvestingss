"""Route a LinkedIn Feed vendor lead to its market's client tenant.

LinkedIn Home Feed / post-search leads are harvested by one run but belong to
two different client markets: US requirements (C2C / W2 / H1B, USD rates) go to
``client_us`` and Indian ones (LPA / INR, Indian cities) go to ``client_in``.
:func:`classify_feed_lead_tenant` decides, in order:

1. **Phone** (strongest signal) — ``+1`` / ``001`` / ``1`` + 10 digits → US
   (NANP also covers Canada, which can't be told apart from the number);
   ``+91`` / ``0091`` / ``91`` + 10-digit mobile / ``0`` + 10-digit mobile → IN.
   Other country codes and bare 10-digit numbers are ambiguous → no signal.
2. **Location** — app/core/location.py's parsed country (United States / India),
   else a word scan for US states (names, and ``, TX``-style codes), major US
   cities, "USA"/"United States"/"US"; Indian states, major Indian cities,
   "India"/"Pan India". A location naming BOTH markets is no signal.
   If phone and location disagree, the phone wins.
3. **LLM** — the feed extraction's ``market`` field ("US" | "IN" | "unknown"),
   judged from cues like USD vs INR/LPA rates, W2/C2C/H1B/GC, EST/PST vs IST.
4. **Fallback** — the run's own tenant (pre-classification behaviour). For an
   ``internal`` run that means unclassifiable leads stay ``internal`` (which sees
   every tenant anyway); classified leads still go to the client tenant.

Pure — no I/O — so it is unit-tested directly (tests/test_services/
test_feed_tenant_classifier.py).
"""
from __future__ import annotations

import re

from app.core.location import (
    _INDIAN_CITIES,
    _US_STATE_CODES,
    _US_STATE_NAMES,
    parse_location,
)

US = "US"
IN = "IN"
UNKNOWN = "unknown"

MARKET_TENANTS: dict[str, str] = {US: "client_us", IN: "client_in"}

# ── Phone ─────────────────────────────────────────────────────────────────────

_PHONE_NOISE_RE = re.compile(r"[\s().\-/]")
_IN_MOBILE_RE = re.compile(r"^[6-9]\d{9}$")


def market_from_phone(phone: str | None) -> str:
    """US / IN from a phone's country code, else UNKNOWN (missing, other country,
    or an ambiguous bare 10-digit number)."""
    raw = _PHONE_NOISE_RE.sub("", str(phone or "").strip())
    if not raw:
        return UNKNOWN
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    if raw.startswith("+"):
        digits = raw[1:]
        if not digits.isdigit():
            return UNKNOWN
        if digits.startswith("91") and len(digits) == 12:
            return IN
        if digits.startswith("1") and len(digits) == 11:
            return US
        return UNKNOWN
    if not raw.isdigit():
        return UNKNOWN
    if len(raw) == 12 and raw.startswith("91") and _IN_MOBILE_RE.match(raw[2:]):
        return IN
    if len(raw) == 11 and raw.startswith("0") and _IN_MOBILE_RE.match(raw[1:]):
        return IN  # Indian trunk prefix: 0 98xxxxxxxx
    if len(raw) == 11 and raw.startswith("1") and raw[1] in "23456789":
        return US  # 1 + NANP area code (area codes never start with 0/1)
    return UNKNOWN


# ── Location ──────────────────────────────────────────────────────────────────

_US_CITIES = (
    "new york", "nyc", "new york city", "san francisco", "bay area", "los angeles",
    "san jose", "san diego", "seattle", "redmond", "bellevue", "portland", "boston",
    "chicago", "dallas", "plano", "irving", "frisco", "houston", "austin",
    "san antonio", "atlanta", "alpharetta", "charlotte", "raleigh", "durham",
    "phoenix", "scottsdale", "denver", "salt lake city", "miami", "tampa",
    "orlando", "jacksonville", "detroit", "minneapolis", "st. louis", "st louis",
    "kansas city", "columbus", "cincinnati", "cleveland", "pittsburgh",
    "philadelphia", "baltimore", "richmond", "jersey city", "newark", "princeton",
    "edison", "iselin", "hartford", "nashville", "indianapolis", "milwaukee",
    "sacramento", "sunnyvale", "santa clara", "mountain view", "palo alto",
    "cupertino", "fremont", "las vegas", "reston", "mclean", "herndon",
    "silicon valley", "washington dc", "washington, dc", "washington d.c.",
)
_US_TERMS = ("usa", "u.s.a", "u.s.", "united states", "united states of america")

_IN_STATES = (
    "karnataka", "tamil nadu", "tamilnadu", "telangana", "maharashtra",
    "andhra pradesh", "kerala", "gujarat", "haryana", "uttar pradesh",
    "west bengal", "rajasthan", "punjab", "odisha", "orissa", "madhya pradesh",
    "bihar", "jharkhand", "chhattisgarh", "uttarakhand", "goa", "assam",
    "delhi ncr", "ncr", "himachal pradesh", "jammu", "kashmir",
)
_IN_TERMS = ("india", "bharat", "pan india", "pan-india")


def _alt(terms) -> str:
    """Longest-first, escaped alternation (so "new york city" beats "new york")."""
    return "|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True))


_US_WORD_RE = re.compile(
    r"(?<![\w.])(?:" + _alt([*_US_TERMS, *_US_CITIES, *(n.lower() for n in _US_STATE_NAMES)])
    + r")(?![\w])",
    re.IGNORECASE,
)
# "US" only as an upper-case standalone token ("Remote - US", "Remote (US)"), never
# the English word "us".
_US_TOKEN_RE = re.compile(r"(?<![A-Za-z])US(?![A-Za-z])")
# A 2-letter state code in the ", TX" / "Dallas TX" position. Weak: ignored when the
# location also names India ("Chennai, TN" is Tamil Nadu). "IN" is never a state
# code here (India), and English-looking codes (OR/ME/OK/HI) count only after a comma.
_AMBIGUOUS_CODES = {"IN", "OR", "ME", "OK", "HI"}
_US_CODE_RE = re.compile(
    r",\s*(?:" + "|".join(c for c in _US_STATE_CODES if c != "IN") + r")(?![A-Za-z])"
    r"|[A-Z][a-z]+\s(?:" + "|".join(c for c in _US_STATE_CODES if c not in _AMBIGUOUS_CODES)
    + r")(?![A-Za-z])"
)
_IN_WORD_RE = re.compile(
    r"(?<![\w])(?:" + _alt([*_IN_TERMS, *_IN_STATES, *_INDIAN_CITIES]) + r")(?![\w])",
    re.IGNORECASE,
)


def market_from_location(location: str | None) -> str:
    """US / IN from a free-text location, else UNKNOWN (blank, "Remote", another
    country, or a location naming both markets)."""
    text = str(location or "").strip()
    if not text:
        return UNKNOWN
    is_in = bool(_IN_WORD_RE.search(text))
    country = parse_location(text)[2]
    if country == "India":
        return IN
    # parse_location reads a trailing 2-letter code as a US state, so "Chennai, TN"
    # parses as Tennessee — trust its US answer only when no Indian place is named.
    if country == "United States" and not is_in:
        return US
    is_us = bool(_US_WORD_RE.search(text) or _US_TOKEN_RE.search(text))
    if not is_us and not is_in:
        is_us = bool(_US_CODE_RE.search(text))  # weak: only when India isn't named
    if is_us and not is_in:
        return US
    if is_in and not is_us:
        return IN
    return UNKNOWN


# ── LLM field ─────────────────────────────────────────────────────────────────

_LLM_ALIASES = {
    "us": US, "usa": US, "u.s.": US, "united states": US, "north america": US,
    "in": IN, "india": IN, "ind": IN,
}


def normalize_market(value: object) -> str:
    """The LLM's ``market`` value as US / IN / UNKNOWN (anything unrecognised)."""
    return _LLM_ALIASES.get(str(value or "").strip().lower(), UNKNOWN)


# ── Decision ──────────────────────────────────────────────────────────────────

def classify_feed_lead_market(
    *, phone: str | None = None, location: str | None = None, llm_market: object = None,
) -> tuple[str, str]:
    """(market, signal) — market is US / IN / UNKNOWN; signal names what decided it
    ("phone", "location", "llm" or "none")."""
    by_phone = market_from_phone(phone)
    if by_phone != UNKNOWN:
        return by_phone, "phone"  # wins over a conflicting location
    by_location = market_from_location(location)
    if by_location != UNKNOWN:
        return by_location, "location"
    by_llm = normalize_market(llm_market)
    if by_llm != UNKNOWN:
        return by_llm, "llm"
    return UNKNOWN, "none"


def classify_feed_lead_tenant(
    *,
    fallback_tenant: str,
    phone: str | None = None,
    location: str | None = None,
    llm_market: object = None,
) -> tuple[str, str]:
    """(tenant_id, signal) for one feed lead: the market's client tenant
    (client_us / client_in), else ``fallback_tenant`` (the run's tenant) with
    signal "fallback"."""
    market, signal = classify_feed_lead_market(
        phone=phone, location=location, llm_market=llm_market,
    )
    if market in MARKET_TENANTS:
        return MARKET_TENANTS[market], signal
    return fallback_tenant, "fallback"
