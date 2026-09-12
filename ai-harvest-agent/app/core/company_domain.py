"""Company-name → web domain inference.

Apollo's organization-enrichment endpoint keys off a **domain**, not a company
name or LinkedIn company URL. When no domain is already known (e.g. a job with
no Apollo people-match to borrow ``organization.primary_domain`` from), this
module makes a best-effort guess from the company name: a curated known-company
map first, then a heuristic that strips legal/industry suffixes and appends
``.com``.

Heuristic only — a wrong guess just means Apollo returns no match (no job is ever
dropped or mis-tagged on the strength of it). Mirrors the private
``_infer_company_domain`` in app/agents/prospect_intelligence_agent.py; kept here
so the Apollo company-enrichment fallback can reuse it without importing an agent.
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
}

_SUFFIX_RE = re.compile(
    r"\b(inc|ltd|pvt|llc|corp|limited|technologies|tech|solutions|services|"
    r"systems|group|global|digital|software|labs|ai|io|infotech|analytics|"
    r"financial|payments|bank|realty|reality|plc|research|engg|engineering)\b"
)


def infer_company_domain(company_name: str) -> tuple[str, str]:
    """Return ``(domain, website_url)`` for a company name, or ``("", "")`` when
    nothing can be inferred. Checks the known map (exact/substring) first, then
    the suffix-stripping heuristic."""
    slug = (company_name or "").lower().strip()
    if not slug:
        return "", ""
    for known, domain in _KNOWN_DOMAINS.items():
        if known == slug or known in slug or slug in known:
            return domain, f"https://www.{domain}"
    cleaned = _SUFFIX_RE.sub("", slug)
    cleaned = re.sub(r"[^a-z0-9]", "", cleaned)
    if cleaned:
        domain = f"{cleaned}.com"
        return domain, f"https://www.{domain}"
    return "", ""
