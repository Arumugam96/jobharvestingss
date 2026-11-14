"""Company-name → web domain inference.

Apollo's organization-enrichment endpoint keys off a **domain**, not a company
name or LinkedIn company URL. When no domain is already known (e.g. a job with
no Apollo people-match to borrow ``organization.primary_domain`` from), this
module makes a best-effort guess from the company name: a curated known-company
map (exact normalized name) first, then a heuristic that strips trailing
legal/industry suffixes and appends ``.com``.

Heuristic only — a wrong guess just means Apollo returns no match (no job is ever
dropped or mis-tagged on the strength of it). The single implementation:
``_infer_company_domain`` in app/agents/prospect_intelligence_agent.py delegates
here, so the Apollo company-enrichment fallback needs no agent import.
"""
from __future__ import annotations

import re

# Curated company → primary domain map (lower-cased company slug → domain). A
# subset focused on the companies seen across harvested jobs; extend as needed.
_KNOWN_DOMAINS: dict[str, str] = {
    "oracle":                         "oracle.com",
    "ibm":                            "ibm.com",
    "adobe":                          "adobe.com",
    "morgan stanley":                 "morganstanley.com",
    "razorpay":                       "razorpay.com",
    "paytm":                          "paytm.com",
    "meesho":                         "meesho.com",
    "fractal analytics":              "fractal.ai",
    "fractal":                        "fractal.ai",
    "hexaware":                       "hexaware.com",
    "cyient":                         "cyient.com",
    "collabera":                      "collabera.com",
    "nttdata":                        "nttdata.com",
    "ntt data":                       "nttdata.com",
    "itc infotech":                   "itcinfotech.com",
    "publicis sapient":               "publicissapient.com",
    "nielsen iq":                     "nielseniq.com",
    "guidewire":                      "guidewire.com",
    "coupang":                        "coupang.com",
    "clearwater analytics":           "clearwateranalytics.com",
    # Merged from the former private copy in prospect_intelligence_agent.py.
    "oracle financial services software limited": "oracle.com",
    "neilson iq":                     "nielseniq.com",
    "guidewire software":             "guidewire.com",
    "kiya ai":                        "kiya.ai",
    "fino payments bank":             "finobank.com",
    "fino":                           "finobank.com",
    "odessa":                         "odessa.io",
    "amadeus labs":                   "amadeus.com",
    "amadeus":                        "amadeus.com",
    "ness digital engineering":       "ness.com",
    "ness digital engg":              "ness.com",
    "lentra ai":                      "lentra.ai",
    "lentra":                         "lentra.ai",
    "perfios":                        "perfios.com",
    "mastek":                         "mastek.com",
    "ciklum":                         "ciklum.com",
    "e2 open":                        "e2open.com",
    "dxc technologies":               "dxc.com",
    "dxc":                            "dxc.com",
    "niyoto infotech":                "niyoto.com",
    "hashroot":                       "hashroot.in",
    "m360 research":                  "m360research.com",
    "vlink":                          "vlinkinfo.com",
    "ema unlimited":                  "ema-unlimited.com",
    "awign":                          "awign.com",
    "hurix":                          "hurix.com",
    "ig group":                       "iggroup.com",
    "intuitive ai":                   "intuitive.ai",
    "nucleus software":               "nucleussoftware.com",
    "i merit":                        "imerit.net",
    "imerit":                         "imerit.net",
    "global foundries":               "globalfoundries.com",
    "global foundaries":              "globalfoundries.com",
    "global data":                    "globaldata.com",
    "linde":                          "linde.com",
    "ybrant digital":                 "ybrantdigital.com",
    "trilogy":                        "trilogy.com",
    "the hartford":                   "thehartford.com",
    "triotree technologies":          "triotree.com",
    "apisero":                        "apisero.com",
    "hexagon":                        "hexagon.com",
    "ideagen":                        "ideagen.com",
    "fleetx io":                      "fleetx.io",
    "fleetx":                         "fleetx.io",
    "vikram solar":                   "vikramsolar.com",
    "markets and markets":            "marketsandmarkets.com",
    "adani realty":                   "adanirealty.com",
    "adani reality":                  "adanirealty.com",
    "lpl financial":                  "lpl.com",
    "paychex":                        "paychex.com",
}

# Trailing legal-entity / locale words dropped before any lookup ("Oracle India
# Pvt Ltd" -> "oracle"). Only removed from the END of the name.
_LEGAL_TAIL = frozenset({
    "inc", "ltd", "pvt", "private", "limited", "llc", "llp", "corp", "corporation",
    "plc", "co", "company", "india",
})

# Industry words the heuristic drops from the END of the name ("Infosys
# Technologies" -> infosys.com). Leading/middle words are kept, so "Tech Data
# Corp" -> techdata.com and "Global Data Solutions" -> globaldata.com.
_INDUSTRY_TAIL = frozenset({
    "technologies", "tech", "solutions", "services", "systems", "group", "global",
    "digital", "software", "labs", "ai", "io", "infotech", "analytics", "financial",
    "payments", "bank", "realty", "reality", "research", "engg", "engineering",
})

# Generic dictionary words: a multi-word name that reduces to just one of these
# would guess somebody else's domain ("Data Systems" -> data.com), so no guess.
_GENERIC_WORDS = frozenset({
    "data", "info", "cloud", "net", "web", "soft", "smart", "tech", "digital",
    "global", "it", "consulting", "business", "network", "computer", "india",
    "international", "national", "united", "general", "first", "prime", "the",
    "systems", "services", "solutions", "software", "group", "health", "energy",
})


def _name_tokens(company_name: str) -> list[str]:
    """Lower-case alphanumeric word tokens of a company name ("&" -> "and"),
    with trailing legal/locale words (_LEGAL_TAIL) removed."""
    tokens = re.findall(r"[a-z0-9]+", (company_name or "").lower().replace("&", " and "))
    while tokens and tokens[-1] in _LEGAL_TAIL:
        tokens.pop()
    return tokens


def _normalize_name(company_name: str) -> str:
    """Exact-lookup key for a company name: its _name_tokens joined by spaces."""
    return " ".join(_name_tokens(company_name))


_KNOWN_BY_KEY: dict[str, str] = {_normalize_name(k): v for k, v in _KNOWN_DOMAINS.items()}


def known_company_domain(company_name: str) -> str:
    """The curated domain for a company (exact normalized-name hit in
    _KNOWN_DOMAINS), or "" when the name is not in the map. Unlike
    infer_company_domain this never guesses, so a non-empty result can be
    treated as a confirmed domain."""
    return _KNOWN_BY_KEY.get(_normalize_name(company_name), "")


def infer_company_domain(company_name: str) -> tuple[str, str]:
    """Return ``(domain, website_url)`` for a company name, or ``("", "")`` when
    nothing can be inferred.

    Checks the known map by EXACT normalized name (lower-case, punctuation and
    trailing legal words such as "Pvt Ltd" / "Inc" removed), never by substring.
    Otherwise strips trailing industry words ("Technologies", "Solutions") and
    appends ``.com``; returns nothing when fewer than two characters are left
    or when a multi-word name reduces to one generic word (data.com). The result is a
    GUESS: callers must not treat it as a confirmed domain."""
    tokens = _name_tokens(company_name)
    if not tokens:
        return "", ""
    known = _KNOWN_BY_KEY.get(" ".join(tokens))
    if known:
        return known, f"https://www.{known}"
    core = list(tokens)
    while core and core[-1] in _INDUSTRY_TAIL:
        core.pop()
    if not core or (len(tokens) > 1 and len(core) == 1 and core[0] in _GENERIC_WORDS):
        return "", ""
    if len("".join(core)) < 2:
        return "", ""
    domain = f"{''.join(core)}.com"
    return domain, f"https://www.{domain}"


# Display placeholders the scrapers substitute when a card carries no company name.
# They are NOT a company: never enrich them, never key a company cache/dedup on them.
_PLACEHOLDER_COMPANIES: frozenset[str] = frozenset({
    "unknown company", "unknown", "company", "not disclosed", "not specified",
    "confidential", "n/a", "na", "-",
})


def is_placeholder_company(name: str | None) -> bool:
    """True when `name` is blank or a scraper placeholder such as "Unknown Company"."""
    return (name or "").strip().lower() in _PLACEHOLDER_COMPANIES or not (name or "").strip()
