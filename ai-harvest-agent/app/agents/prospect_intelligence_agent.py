"""
Prospect / Recruiter Intelligence Agent

Enrichment pipeline per recruiter
───────────────────────────────────
Step 1  Company website scraping       → VERIFIED email / phone
Step 2  LinkedIn profile visit         → PUBLIC email / phone (click Contact Info)
Step 3  Naukri cross-source search     → PUBLIC email / phone
Step 4  Hierarchy discovery            → DuckDuckGo TA/HR leadership search
Step 5  Confidence scoring             → High / Medium / Low

CRITICAL RULES
──────────────
• Email and phone are NEVER predicted or generated.
• Only data actually scraped from a public source is stored.
• email_status:  VERIFIED | PUBLIC | NOT_FOUND   (no PREDICTED)
• phone_status:  VERIFIED | PUBLIC | NOT_FOUND   (no PREDICTED)
• Uses PersistentBrowserManager — Chrome already authenticated, no login automation.
• Debug JSON saved per run to data/results/lead_intelligence/debug/
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from app.core.company_domain import infer_company_domain, known_company_domain
from app.core.contact_grounding import ground_email, ground_phone
from app.core.contact_normalize import normalize_email, normalize_phone
from app.models.harvest_run import LlmCallType
from app.scrapers.human import human_click
from app.models.prospect_models import (
    ProspectIntelligenceResult,
    ProspectRecord,
    ProspectResult,
)

logger = structlog.get_logger(__name__)

_OUTPUT_DIR           = Path("data/results/lead_intelligence")
_DEBUG_DIR            = _OUTPUT_DIR / "debug"
_INTERMEDIATE_DIR     = _OUTPUT_DIR / "intermediate"
_INTERMEDIATE_EVERY   = 25
_DEFAULT_CONCURRENCY  = 2

# ── Email / phone regexes ──────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r'\b([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b')

_SKIP_EMAIL_DOMAINS = {
    "linkedin.com", "sentry.io", "example.com", "gmail.com", "yahoo.com",
    "hotmail.com", "noreply", "no-reply", "notifications", "support",
}

_PHONE_RE = re.compile(
    r'(?:\+91[\s\-]?\d{5}[\s\-]?\d{5}'           # +91 Indian mobile
    r'|\+\d{1,3}[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{4}'  # international
    r'|0\d{2,4}[\s\-]\d{6,8})'                   # Indian landline
)

# ── Department keyword map ─────────────────────────────────────────────────────

_DEPT_MAP: list[tuple[list[str], str]] = [
    (["talent acquisition", "ta ", "talent", "recruiter", "recruitment", "sourcing"], "Talent Acquisition"),
    (["human resources", "hr ", "hrbp", "people success", "people & culture", "people operations"], "Human Resources"),
    (["chro", "chief people", "chief hr"], "HR Leadership"),
    (["technology", "engineering", "software", "tech"], "Technology"),
    (["finance", "cfo", "accounts", "accounting"], "Finance"),
    (["marketing", "brand", "growth"], "Marketing"),
    (["sales", "business development", "bd ", "revenue", "account manager"], "Sales & BD"),
    (["operations", "ops "], "Operations"),
    (["legal", "compliance"], "Legal & Compliance"),
    (["vendor", "vendor management"], "Vendor Management"),
    (["director", "vp", "vice president", "svp", "evp", "managing director", "md"], "Leadership"),
    (["ceo", "founder", "coo", "cto"], "CXO"),
]

# ── Position level map ────────────────────────────────────────────────────────
# Ordered most-specific first so the first match wins.

_POSITION_LEVEL_MAP: list[tuple[list[str], str]] = [
    (["chro", "chief human resources officer", "chief people officer", "chief hr"], "CHRO"),
    (["founder", "co-founder", "co founder"], "Founder"),
    (["senior vice president", "svp", "executive vice president", "evp"], "VP"),
    (["vice president", "vp of", " vp "], "VP"),
    (["associate director"], "Associate Director"),
    (["director"], "Director"),
    (["head of", "head -", "head,", "head &"], "Head"),
    (["senior manager", "sr. manager", "sr manager", "senior talent acquisition manager"], "Senior Manager"),
    (["manager"], "Manager"),
    (["senior recruiter", "sr. recruiter", "sr recruiter", "senior talent acquisition specialist", "senior ta"], "Senior Recruiter"),
    (["talent acquisition specialist", "ta specialist", "talent acquisition associate"], "Talent Acquisition Specialist"),
    (["recruiter", "talent acquisition"], "Recruiter"),
]

# ── Hiring domain map ─────────────────────────────────────────────────────────

_HIRING_DOMAIN_MAP: list[tuple[list[str], str]] = [
    (["artificial intelligence", " ai ", "machine learning", " ml ", "deep learning",
       "nlp", "genai", "generative ai", "llm", "computer vision", "data scientist"], "AI/ML"),
    (["cloud", " aws ", " azure ", " gcp ", "google cloud", "devops", "kubernetes",
       "docker", "site reliability", " sre "], "Cloud/DevOps"),
    ([" sap "], "SAP"),
    (["java", "spring boot", "j2ee", "microservices"], "Java"),
    (["data engineer", "data engineering", "big data", " spark ", " kafka ",
       "databricks", "etl "], "Data Engineering"),
    (["data science", "data analyst", "business intelligence", " bi ",
       "analytics", "tableau", "power bi"], "Data & Analytics"),
    (["full stack", "fullstack", " react ", " angular ", "nodejs",
       "frontend developer", "backend developer"], "Full Stack"),
    (["cybersecurity", "information security", "infosec", "penetration test"], "Cybersecurity"),
    (["salesforce", " crm ", "servicenow"], "Salesforce/CRM"),
    (["embedded", "firmware", " iot ", "vlsi", "fpga"], "Embedded/IoT"),
    (["product manager", "product management", "product owner"], "Product Management"),
    (["fintech", "payments", "lending", "treasury", "banking technology"], "FinTech"),
    (["healthcare", "medtech", "pharma", "biotech"], "Healthcare"),
    (["mobile developer", "android developer", "ios developer", "react native", "flutter"], "Mobile"),
    (["quality assurance", "automation test", "selenium", " qa engineer"], "QA/Testing"),
    (["erp", "oracle ebs", "peoplesoft"], "ERP"),
    (["network engineer", " cisco ", "routing switching"], "Networking"),
    (["mainframe", "cobol", "as400"], "Mainframe"),
]

# Pages to try on a company website for contact details
_CONTACT_PATHS = ["/contact", "/contact-us", "/team", "/about/leadership", "/about", "/management"]

# Selectors to try for the LinkedIn "Contact info" modal trigger
_LI_CONTACT_SELECTORS = [
    'a[href*="overlay/contact-info"]',
    'a[id*="contact-info"]',
    'a[data-control-name*="contact"]',
    '[class*="contact-info"] a',
    'a.pv-top-card--list-bullet',
]

# Selectors for the contact modal content
_LI_MODAL_SELECTORS = [
    '.artdeco-modal__content',
    '.pv-contact-info__contact-type',
    '[class*="contact-info__contact-type"]',
    '.ci-phone', '.ci-email',
    '[data-view-name*="contact"]',
]


# ═══════════════════════════════════════════════════════════════════════════════
# Pure helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _load_prospects(xlsx_path: str) -> list[ProspectRecord]:
    """Read prospects.xlsx with forward-fill company name."""
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("openpyxl required: pip install openpyxl")

    path = Path(xlsx_path)
    if not path.exists():
        raise FileNotFoundError(f"Prospects file not found: {xlsx_path}")

    wb        = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    ws        = wb.active
    all_rows  = list(ws.iter_rows(values_only=True))
    wb.close()

    if not all_rows:
        return []

    header     = []
    data_start = 0
    for i, row in enumerate(all_rows):
        non_empty = [c for c in row if c is not None and str(c).strip()]
        if len(non_empty) > 1:
            header     = [str(c).strip().lower() if c else "" for c in row]
            data_start = i + 1
            break

    if not header:
        return []

    def _find_col(*kws: str) -> int:
        """Index of the first header containing any keyword (tried in priority order), else -1."""
        for kw in kws:
            for i, h in enumerate(header):
                if kw in h:
                    return i
        return -1

    col_company  = _find_col("client name", "company name", "company", "client")
    col_person   = _find_col("poc name", "person name", "contact name", "poc", "person")
    col_desig    = _find_col("designation", "title", "role")
    col_linkedin = _find_col("linkedin", "profile url", "url", "profile")

    if col_person == -1:
        return []

    def _cell(row: tuple, col: int) -> str:
        """Stripped cell text for col, or "" when out of range, empty, "none", "n/a" or "-"."""
        if col < 0 or col >= len(row) or row[col] is None:
            return ""
        val = str(row[col]).strip()
        return "" if val.lower() in ("none", "n/a", "-") else val

    records:      list[ProspectRecord] = []
    last_company: str                  = ""

    for row_i, row in enumerate(all_rows[data_start:], start=data_start + 1):
        person = _cell(row, col_person)
        if not person:
            continue
        company = _cell(row, col_company)
        if company:
            last_company = company
        records.append(ProspectRecord(
            company_name      = last_company,
            person_name       = person,
            designation       = _cell(row, col_desig),
            existing_linkedin = _cell(row, col_linkedin),
            row_index         = row_i,
        ))

    logger.info("prospects_loaded", total=len(records), path=xlsx_path)
    return records


def _infer_company_domain(company_name: str) -> tuple[str, str]:
    """Return (domain, website_url) — a GUESS from the company name.

    Delegates to app.core.company_domain.infer_company_domain (exact known-name
    lookup, then the trailing-suffix heuristic); ("", "") when nothing fits."""
    return infer_company_domain(company_name or "")


def _infer_department(designation: str, headline: str = "") -> str:
    """Map designation + headline keywords to a department via _DEPT_MAP ("" if none match)."""
    text = (designation + " " + headline).lower()
    for keywords, dept in _DEPT_MAP:
        if any(kw in text for kw in keywords):
            return dept
    return ""


def _classify_position_level(designation: str, headline: str = "") -> str:
    """Map designation / LinkedIn headline to a standardised seniority tier."""
    text = f" {designation} {headline} ".lower()
    for keywords, level in _POSITION_LEVEL_MAP:
        if any(kw in text for kw in keywords):
            return level
    return "NOT_FOUND"


def _classify_hiring_domain(designation: str, headline: str, job_titles: list[str]) -> str:
    """
    Classify the technology / functional domain the recruiter hires for.
    Uses designation + LinkedIn headline + job titles posted in the harvest run.
    Returns up to 3 comma-separated domains, or NOT_FOUND.
    """
    text = f" {designation} {headline} {' '.join(job_titles)} ".lower()
    found: list[str] = []
    seen:  set[str]  = set()
    for keywords, domain in _HIRING_DOMAIN_MAP:
        if domain not in seen and any(kw in text for kw in keywords):
            found.append(domain)
            seen.add(domain)
        if len(found) >= 3:
            break
    return ", ".join(found) if found else "NOT_FOUND"


async def _extract_linkedin_profile_metadata(page: Any) -> dict:
    """
    Scrape extended profile metadata from an already-loaded LinkedIn profile page.

    Called AFTER _extract_linkedin_contact_info (no extra navigation needed).

    Returns dict with keys:
        employment_type    — Full-time | Contract | Part-time | Internship
        years_in_company   — "3 yrs 6 mos" (current-role tenure from experience section)
        overall_experience — summed career duration across all experience items
        company_industry   — LinkedIn industry taxonomy string
        company_size       — standard LinkedIn employee-range band

    All fields default to NOT_FOUND.
    """
    out: dict[str, str] = {
        "employment_type":    "NOT_FOUND",
        "years_in_company":   "NOT_FOUND",
        "overall_experience": "NOT_FOUND",
        "company_industry":   "NOT_FOUND",
        "company_size":       "NOT_FOUND",
    }
    try:
        body = await page.inner_text("body")

        # Employment type (appears near the role title in experience section)
        emp_m = re.search(
            r'\b(Full[\-\s]?time|Part[\-\s]?time|Contract|Freelance|Self[\-\s]?employed|Internship|Apprenticeship)\b',
            body, re.I,
        )
        if emp_m:
            out["employment_type"] = re.sub(r'[\-\s]+', '-', emp_m.group(1)).title()

        # Years in current company — first "– Present" duration on the page
        cur_m = re.search(
            r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s*[–\-]+\s*Present'
            r'\s*·\s*(\d+\s*yr?s?(?:\s+\d+\s*mos?)?|\d+\s*mos?)',
            body, re.I,
        )
        if cur_m:
            out["years_in_company"] = cur_m.group(1).strip()

        # Overall experience — sum all experience-item duration tokens
        total_months = 0
        for dm in re.finditer(
            r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s*[–\-]+\s*'
            r'(?:Present|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4})'
            r'\s*·\s*(\d+)\s*yr?s?(?:\s+(\d+)\s*mos?)?',
            body, re.I,
        ):
            total_months += int(dm.group(1)) * 12 + int(dm.group(2) or 0)
        if total_months > 0:
            yrs, mos = divmod(total_months, 12)
            out["overall_experience"] = f"{yrs} yrs {mos} mos" if mos else f"{yrs} yrs"

        # Company size — standard LinkedIn employee-range bands
        size_m = re.search(
            r'(10,001\+|5,001[–\-]10,000|1,001[–\-]5,000|501[–\-]1,000|'
            r'201[–\-]500|51[–\-]200|11[–\-]50|2[–\-]10)\s*employees?',
            body, re.I,
        )
        if size_m:
            out["company_size"] = size_m.group(0).strip()

        # Company industry — match LinkedIn taxonomy terms in page text
        _LINKEDIN_INDUSTRIES = [
            "IT Services and IT Consulting", "Software Development",
            "Technology, Information and Internet", "Financial Services",
            "Banking", "Insurance", "Staffing and Recruiting",
            "Human Resources Services", "Management Consulting",
            "Business Consulting and Services", "Computer and Network Security",
            "Semiconductor Manufacturing", "Telecommunications",
            "E-Learning", "Education Administration Programs",
            "Retail", "Manufacturing", "Automotive",
            "Hospitals and Health Care", "Pharmaceutical Manufacturing",
            "Biotechnology Research", "Oil and Gas", "Construction",
            "Real Estate", "Advertising Services", "Entertainment Providers",
            "Food and Beverage Services", "Airlines and Aviation",
            "Transportation, Logistics, Supply Chain and Storage",
            "Information Technology and Services",
        ]
        body_lower = body.lower()
        for industry in _LINKEDIN_INDUSTRIES:
            if industry.lower() in body_lower:
                out["company_industry"] = industry
                break

    except Exception as exc:
        logger.debug("linkedin_metadata_extract_failed", error=str(exc))

    return out


def _normalize_name(name: str) -> str:
    """Lower-case, trim and collapse internal whitespace for name/company comparisons."""
    return re.sub(r"\s+", " ", name.strip().lower())


def _score_confidence(result: ProspectResult, company_name: str, designation: str) -> str:
    """
    High   — LinkedIn confirmed + VERIFIED or PUBLIC email + company match
    Medium — LinkedIn confirmed + company match (no email)
    Low    — No LinkedIn profile resolved, or no company match

    company match = a company-name word (>3 chars) appears as a whole word in
    the LinkedIn headline, or the domain is CONFIRMED: it is the curated domain
    for this company, or the found email is on it. A merely guessed
    company_domain (always filled by the name heuristic) does not count.
    """
    if not result.linkedin_url:
        return "Low"

    headline_words = set(re.findall(r"[a-z0-9]+", _normalize_name(result.linkedin_headline)))
    domain = (result.company_domain or "").lower()
    email_host = (result.official_email_id or "").lower().rpartition("@")[2]
    company_match = bool(
        any(
            tok in headline_words
            for tok in re.findall(r"[a-z0-9]+", _normalize_name(company_name))
            if len(tok) > 3
        )
        or (domain and (domain == known_company_domain(company_name) or email_host == domain))
    )

    has_real_email = result.email_status in ("VERIFIED", "PUBLIC")

    if company_match and has_real_email:
        return "High"
    if company_match:
        return "Medium"
    return "Low"


_LI_PROFILE_RE = re.compile(r'linkedin\.com/in/([a-zA-Z0-9\-_%]+)', re.IGNORECASE)


def _canonicalize_linkedin_profile_url(url: str) -> str:
    """Reduce any LinkedIn profile URL to https://www.linkedin.com/in/<slug>/,
    dropping /recent-activity/, /detail/, /overlay/… path suffixes and query
    strings that would otherwise load an activity/feed page instead of the
    profile top-card. Navigating to such a page makes the Contact-info modal
    selectors miss, and the full-body fallback scan then picks up the FIRST
    email/phone on the page — i.e. the first member in the activity feed —
    rather than the target person's own contact. Non-`/in/` URLs (company/
    school pages, malformed links) are returned untouched."""
    m = _LI_PROFILE_RE.search(url or "")
    if not m:
        return url
    slug = m.group(1).split("?")[0]
    return f"https://www.linkedin.com/in/{slug}/"


def _extract_email_from_text(text: str, company_domain: str = "") -> str:
    """
    Extract a work email from arbitrary text.
    Preference order:
      1. Email matching company domain (most authoritative)
      2. Any non-generic professional email
    Returns "" if none found or all matches are system/platform emails.
    """
    best_domain_match = ""
    first_generic     = ""

    for m in _EMAIL_RE.finditer(text):
        candidate = m.group(1).lower()
        host      = candidate.split("@")[1] if "@" in candidate else ""

        # Skip known system/platform domains
        if any(s in host for s in _SKIP_EMAIL_DOMAINS):
            continue
        # Skip generic local-parts
        local = candidate.split("@")[0]
        if local in {"info", "contact", "hr", "careers", "support", "admin", "hello", "hello", "enquiry"}:
            continue

        if company_domain and host == company_domain:
            best_domain_match = candidate
            break   # best possible match

        if not first_generic:
            first_generic = candidate

    return best_domain_match or first_generic


def _extract_phone_from_text(text: str) -> str:
    """Return the first Indian/international phone number in text with whitespace removed.

    Returns "" when no match. Only numbers literally present in the text are returned.
    """
    m = _PHONE_RE.search(text)
    return re.sub(r'\s+', '', m.group(0)) if m else ""


# ── Persistence ────────────────────────────────────────────────────────────────

def _save_intermediate(results: list[ProspectResult], batch_num: int, run_id: str) -> None:
    """Checkpoint all results so far to <run_id>_batch_NNN.json; write errors are only logged."""
    try:
        _INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _INTERMEDIATE_DIR / f"{run_id}_batch_{batch_num:03d}.json"
        path.write_text(
            json.dumps({"batch": batch_num, "count": len(results),
                        "results": [r.to_dict() for r in results]},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("intermediate_saved", batch=batch_num, count=len(results))
    except Exception as exc:
        logger.warning("intermediate_save_failed", error=str(exc))


def _save_final_json(results: list[ProspectResult], run_id: str) -> str:
    """Write confidence/contact counts plus every result to <run_id>_lead_intelligence.json.

    Returns the absolute path of the written file.
    """
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path    = _OUTPUT_DIR / f"{run_id}_lead_intelligence.json"
    payload = {
        "run_id":          run_id,
        "total":           len(results),
        "enriched":        sum(1 for r in results if r.linkedin_url or r.company_domain),
        "high":            sum(1 for r in results if r.confidence_score == "High"),
        "medium":          sum(1 for r in results if r.confidence_score == "Medium"),
        "low":             sum(1 for r in results if r.confidence_score == "Low"),
        "verified_emails": sum(1 for r in results if r.email_status == "VERIFIED"),
        "public_emails":   sum(1 for r in results if r.email_status == "PUBLIC"),
        "verified_phones": sum(1 for r in results if r.phone_status == "VERIFIED"),
        "public_phones":   sum(1 for r in results if r.phone_status == "PUBLIC"),
        "no_contact":      sum(
            1 for r in results
            if r.email_status == "NOT_FOUND" and r.phone_status == "NOT_FOUND"
        ),
        "results": [r.to_dict() for r in results],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path.resolve())


def _save_debug_json(results: list[ProspectResult], run_id: str) -> str:
    """
    Save per-recruiter diagnostic JSON to data/results/lead_intelligence/debug/.
    Records: profile_opened, contact_section_found, email_found, phone_found,
             hierarchy_found, audit_log, email_status, phone_status.
    """
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path    = _DEBUG_DIR / f"{run_id}_diagnostics.json"
    records = []
    for r in results:
        records.append({
            "person_name":          r.person_name,
            "company_name":         r.company_name,
            "linkedin_url":         r.linkedin_url,
            "email_status":         r.email_status,
            "phone_status":         r.phone_status,
            "confidence_score":     r.confidence_score,
            "profile_opened":        r.profile_opened,
            "contact_section_found": r.contact_section_found,
            "email_found":           r.email_found,
            "phone_found":           r.phone_found,
            "hierarchy_found":       r.hierarchy_found,
            "audit_log":            r.enrichment_audit,
        })
    path.write_text(
        json.dumps({"run_id": run_id, "total": len(records), "records": records},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("debug_json_saved", path=str(path))
    return str(path.resolve())


def _save_summary(run_id: str, result: "ProspectIntelligenceResult") -> None:
    """Overwrite lead_intelligence_summary.json with this run's summary (errors only logged)."""
    try:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = _OUTPUT_DIR / "lead_intelligence_summary.json"
        path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("summary_save_failed", error=str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# Browser helpers
# ═══════════════════════════════════════════════════════════════════════════════

async def _delay(page: Any, lo: int, hi: int) -> None:
    """Human-like pause (random lo–hi ms) after a page's DOM loads, to reduce the
    bot-detection signal from mechanical, fixed-interval navigation."""
    await page.wait_for_timeout(random.randint(lo, hi))


async def _ddg_search_raw(page: Any, query: str, timeout_ms: int = 12000) -> list[dict]:
    """Execute a DuckDuckGo HTML search. Returns [{href, title, snippet}]."""
    encoded = urllib.parse.quote(query)
    url     = f"https://html.duckduckgo.com/html/?q={encoded}"
    items:  list[dict] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(600)
        links = await page.query_selector_all("a.result__a")
        for link in links[:8]:
            raw_href = await link.get_attribute("href") or ""
            title    = (await link.inner_text()).strip()
            href     = raw_href
            if "uddg=" in href:
                m = re.search(r'uddg=([^&]+)', href)
                if m:
                    href = urllib.parse.unquote(m.group(1))
            snippet = ""
            try:
                result_el = await page.evaluate_handle(
                    "(el) => el.closest('.result, .result--default')", link
                )
                if result_el:
                    snip_el = await result_el.query_selector(".result__snippet")
                    if snip_el:
                        snippet = (await snip_el.inner_text()).strip()
            except Exception:
                pass
            items.append({"href": href, "title": title, "snippet": snippet})
    except Exception as exc:
        logger.debug("ddg_search_failed", query=query[:80], error=str(exc))
    return items


async def _ddg_linkedin_search(page: Any, person_name: str, company_name: str) -> dict:
    """
    Use DuckDuckGo HTML to find a person's LinkedIn /in/ profile URL.
    Returns {linkedin_url, name, headline}. Empty dict if not found.
    """
    query = f'site:linkedin.com/in/ "{person_name}" "{company_name}"'
    items = await _ddg_search_raw(page, query, timeout_ms=12000)

    for item in items:
        m = re.search(r'linkedin\.com/in/([a-zA-Z0-9\-_%]+)', item["href"])
        if not m:
            continue
        slug        = m.group(1).split("?")[0]
        profile_url = f"https://www.linkedin.com/in/{slug}"
        title_clean = item["title"].replace("| LinkedIn", "").strip()
        name        = ""
        headline    = ""
        if " - " in title_clean:
            parts    = title_clean.split(" - ", 1)
            name     = parts[0].strip()
            headline = parts[1].strip()
        else:
            name = title_clean
        return {"linkedin_url": profile_url, "name": name, "headline": headline}

    return {}


_LLM_CONTACT_TEXT_MAX_CHARS = 6_000


async def _llm_extract_contact_fields(
    llm_service: Any, contact_text: str, source_url: str = ""
) -> dict:
    """Let the LLM extract a person's email / phone from their captured
    LinkedIn contact-info text — the authoritative extractor when a service is
    available. More reliable than the regex heuristics, which silently drop
    valid addresses (e.g. anything on gmail.com / yahoo.com via
    _SKIP_EMAIL_DOMAINS, or a phone in a format _PHONE_RE doesn't cover) — the
    exact "email is present but wasn't scraped" case.

    Extraction only: the LLM is instructed to return a field ONLY if it appears
    verbatim in the text, never to guess or construct one (honours the module's
    "email/phone are NEVER predicted" rule), and every value it returns is then
    checked with ground_email / ground_phone against that same text — anything
    not literally present is dropped. Returns {"email": str, "phone": str}
    with "" for any field not present. Best-effort — any LLM failure (including
    provider outage) returns empties so the caller's regex result still stands.
    """
    out = {"email": "", "phone": ""}
    text = (contact_text or "").strip()
    if not llm_service or len(text) < 5:
        return out
    if len(text) > _LLM_CONTACT_TEXT_MAX_CHARS:
        text = text[:_LLM_CONTACT_TEXT_MAX_CHARS]

    schema_description = (
        '{"email": str or null (ONLY if an email address appears verbatim in '
        'the text — never guess or construct one. Return it exactly as written '
        "with no masking; if it is masked/partial (e.g. contains '*'), null), "
        '"phone": str or null (ONLY if a phone number appears verbatim in the '
        'text — never guess or construct one. Return its digits exactly as '
        'written (keep a leading + only if the text has one; never add a '
        "country code); strip spaces, brackets, dashes and masking noise such as "
        "'*' or '\\'. If it is masked, partial, or you are not confident it is "
        'a complete real number, null)}'
    )
    try:
        extracted = await llm_service.extract_json(
            content=text,
            schema_description=schema_description,
            system=(
                "You extract only explicitly-stated contact details from a "
                "LinkedIn profile's contact-info section. Extract ONLY the "
                "profile owner's own email/phone — ignore any address or number "
                "that appears inside reposts, the activity feed, comments, job "
                "posts, or other people's content. Never fabricate, guess, or "
                "construct an email or phone number that is not literally "
                "present in the text. Return a phone's digits as written (never "
                "add a country code), and drop any masking/formatting noise "
                "(asterisks, backslashes, brackets); if a value is masked or "
                "partial, return null for it. Return only the fields in the "
                "schema, no commentary."
            ),
            job_url=source_url or None,
            call_type=LlmCallType.CONTACT_HARVEST,
        )
    except Exception as exc:
        logger.debug("llm_contact_extract_failed", url=source_url, error=str(exc))
        return out

    # Deterministic second layer — reject masked/partial noise, then require the
    # value to be grounded in the very text the LLM saw (never a model invention
    # such as an added country code).
    email = normalize_email(extracted.get("email"))
    phone = normalize_phone(extracted.get("phone"))
    if email:
        email = normalize_email(ground_email(email, [text])[0])
    if phone:
        phone = normalize_phone(ground_phone(phone, [text])[0])
    out["email"] = email or ""
    out["phone"] = phone or ""
    return out


async def _extract_linkedin_contact_info(
    page: Any, linkedin_url: str, company_domain: str = "", llm_service: Any = None
) -> dict:
    """
    Step 2: Visit a LinkedIn profile and extract publicly visible email / phone.

    Strategy
    ────────
    1. Navigate to profile page (URL canonicalised to /in/<slug>).
    2. Try to click the "Contact info" link to open the modal.
    3. Capture the contact-info text (modal, then scoped profile card).
    4. When an `llm_service` is provided, let the LLM decide email / phone from
       that captured text — authoritative over the regex heuristics, which drop
       valid gmail/yahoo addresses and non-matching phone formats. The regex
       result is kept only as a fallback when no LLM is available or it returns
       nothing.

    Returns:
        {email, phone, profile_opened, contact_section_found, headline, location}
    """
    out = {
        "email":                "",
        "phone":                "",
        "profile_opened":        False,
        "contact_section_found": False,
        "headline":              "",
        "location":              "",
    }

    # Master gate — skip the LinkedIn profile visit entirely unless explicitly
    # enabled. OFF by default, so all three call sites (harvest recruiter pass,
    # recruiter_contact_agent, prospect_intelligence_agent) no-op here and never
    # open a /in/ page. Returns the empty-but-well-shaped `out` so callers read
    # profile_opened=False / email="" and move on to their other sources.
    from app.config import get_settings
    if not get_settings().linkedin_contact_scraping:
        logger.info("linkedin_contact_scraping_disabled", linkedin_url=linkedin_url)
        return out

    # Reduce activity/overlay/detail URLs (and query strings) to the canonical
    # /in/<slug> profile — otherwise the page loads a feed and the fallback
    # scan below grabs the first activity-feed member's contact, not this
    # person's. See _canonicalize_linkedin_profile_url.
    linkedin_url = _canonicalize_linkedin_profile_url(linkedin_url)

    contact_text = ""   # captured contact-info text, handed to the LLM extractor

    try:
        resp = await page.goto(linkedin_url, wait_until="domcontentloaded", timeout=20000)
        if not resp or resp.status >= 400:
            return out

        out["profile_opened"] = True
        await _delay(page, 2_000, 5_000)   # human-like pause after DOM load

        # ── Grab headline + location from profile ─────────────────────────────
        for sel in ['h2.text-heading-xlarge', '.pv-top-card--list h2', '.top-card-layout__headline']:
            try:
                el = await page.query_selector(sel)
                if el:
                    out["headline"] = (await el.inner_text()).strip().split("\n")[0]
                    break
            except Exception:
                pass

        for sel in ['.pv-top-card--list-bullet li:first-child', '.top-card-layout__first-subline']:
            try:
                el = await page.query_selector(sel)
                if el:
                    out["location"] = (await el.inner_text()).strip().split("\n")[0]
                    break
            except Exception:
                pass

        # ── Try to click "Contact info" link/button ───────────────────────────
        clicked = False
        for sel in _LI_CONTACT_SELECTORS:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    # Hover-then-click near the element centre rather than an
                    # instantaneous programmatic click (anti-detection).
                    await human_click(page, btn)
                    await page.wait_for_timeout(1800)
                    clicked = True
                    break
            except Exception:
                pass

        # Collect text from the contact modal — the profile owner's own
        # contact-info panel, the authoritative source for email/phone.
        modal_text = ""
        if clicked:
            for sel in _LI_MODAL_SELECTORS:
                try:
                    els = await page.query_selector_all(sel)
                    for el in els:
                        t = (await el.inner_text()).strip()
                        if t:
                            modal_text += t + "\n"
                except Exception:
                    pass

            if modal_text:
                out["contact_section_found"] = True
                contact_text += modal_text + "\n"

        # ── Owner-scoped fallback: ONLY the profile owner's own nodes ──────────
        # Never read `main` / `body` / the activity feed. Those contain reposts
        # and "More profiles for you" entries belonging to OTHER people, whose
        # email/phone would otherwise leak in (the reported bug). We look only at
        # the contact modal (above) and the profile top card here — the owner's
        # own region. If neither yields text we capture nothing and the contact
        # stays NOT_FOUND, which is the correct outcome.
        if not out["email"] or not out["phone"]:
            scoped_text = ""
            for sel in (".pv-top-card", "section.artdeco-card.pv-top-card", ".pv-text-details__left-panel"):
                try:
                    el = await page.query_selector(sel)
                    if el:
                        scoped_text = (await el.inner_text()).strip()
                        if scoped_text:
                            break
                except Exception:
                    pass

            if scoped_text:
                contact_text += scoped_text

        # ── LLM is the SOLE authority for email/phone ─────────────────────────
        # Regex over profile text is not trustworthy (it returns any address
        # appearing anywhere in the captured region), so the LLM decides.
        # `contact_text` now only ever holds the owner's contact modal + top
        # card, so the LLM never sees another person's details. When the LLM
        # returns null, email/phone are correctly left empty (NOT_FOUND).
        if llm_service and contact_text.strip():
            llm = await _llm_extract_contact_fields(llm_service, contact_text, linkedin_url)
            if llm.get("email"):
                out["email"] = llm["email"]
            if llm.get("phone"):
                out["phone"] = llm["phone"]
            if llm.get("email") or llm.get("phone"):
                out["contact_section_found"] = True
        # ── LLM-ONLY MODE: regex fallback disabled ────────────────────────────
        # Per requirement, contact info must be extracted ONLY via the LLM call.
        # The regex-over-modal-text fallback below is intentionally commented out.
        # elif not llm_service and modal_text:
        #     # No-LLM deployments only: fall back to regex, but ONLY over the
        #     # contact modal (never the top card / body) so the leak can't recur.
        #     e = _extract_email_from_text(modal_text, company_domain)
        #     p = _extract_phone_from_text(modal_text)
        #     if e:
        #         out["email"] = e
        #     if p:
        #         out["phone"] = p

    except Exception as exc:
        logger.debug("linkedin_contact_extract_failed", url=linkedin_url, error=str(exc))

    return out


def _email_local_matches_person(local: str, person_name: str) -> bool:
    """True when an email local-part is built from this person's name.

    The local-part's letter runs ("priya.sharma" -> priya, sharma; "psharma")
    must each be a concatenation of WHOLE name parts or their initials, with
    no name part used twice; at least one full name part (3+ letters) must be
    used, and two different name parts when the person has two or more.
    Substrings never count, so "ramesh.iyer" does not match "Ram Kumar".
    """
    parts = re.findall(r"[a-z]+", (person_name or "").lower())
    tokens = re.findall(r"[a-z]+", (local or "").lower())
    if not parts or not tokens:
        return False

    def _split(tok: str, used: frozenset[int]) -> list[tuple[frozenset[int], bool]]:
        """Ways to build `tok` from unused name parts: (parts used, any full part)."""
        if not tok:
            return [(used, False)]
        ways: list[tuple[frozenset[int], bool]] = []
        for i, part in enumerate(parts):
            if i in used:
                continue
            for form, full in ((part, len(part) >= 3), (part[0], False)):
                if tok.startswith(form):
                    for u, f in _split(tok[len(form):], used | {i}):
                        ways.append((u, f or full))
        return ways

    states: list[tuple[frozenset[int], bool]] = [(frozenset(), False)]
    for tok in tokens:
        states = [(u2, f or f2) for u, f in states for u2, f2 in _split(tok, u)]
        if not states:
            return False
    need = min(2, len(parts))
    return any(full and len(used) >= need for used, full in states)


async def _scrape_company_website_contacts(
    page: Any, base_url: str, person_name: str, domain: str
) -> dict:
    """
    Step 1: Scrape company website for a specific person's email and phone.

    Tries: /contact, /contact-us, /team, /about/leadership, /about, /management.

    Email acceptance: an @domain address whose local-part words are WHOLE
    name parts / initials of this person (see _email_local_matches_person) —
    "ramesh.iyer@" never matches "Ram". Generic local-parts (info, hr, …) are
    skipped, and no other address on a team page is taken.

    `phone` is always "": a number on a company page (usually the switchboard)
    is not this person's phone. The caller must only pass a CONFIRMED domain.

    Returns {email, phone, source_page}.
    """

    domain_re = (
        re.compile(rf'\b([a-zA-Z0-9._%+-]+@{re.escape(domain)})\b', re.I)
        if domain else None
    )

    _GENERIC_LOCALS = {"info", "contact", "hr", "careers", "support", "admin",
                       "hello", "enquiry", "recruitment", "jobs", "noreply"}

    for path in _CONTACT_PATHS:
        url = base_url.rstrip("/") + path
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=8000)
            if not resp or resp.status >= 400:
                continue
            await page.wait_for_timeout(400)
            text = await page.inner_text("body")
        except Exception:
            continue

        # Look for domain-matching email with name token (most confident)
        found_email = ""
        if domain_re:
            for m in domain_re.finditer(text):
                local = m.group(1).split("@")[0].lower()
                if local in _GENERIC_LOCALS:
                    continue
                if _email_local_matches_person(local, person_name):
                    found_email = m.group(1).lower()
                    break

        if found_email:
            return {"email": found_email, "phone": "", "source_page": path}

    return {"email": "", "phone": "", "source_page": ""}


async def _search_naukri_contact(
    page: Any, person_name: str, company_name: str, company_domain: str = ""
) -> dict:
    """
    Step 3: Search Naukri via DuckDuckGo for a recruiter profile.
    Visit the profile page if found and extract any public contact info.

    Returns {email, phone, profile_url}.
    """
    out = {"email": "", "phone": "", "profile_url": ""}

    query = f'site:naukri.com/mnjuser "{person_name}" "{company_name}"'
    items = await _ddg_search_raw(page, query, timeout_ms=10000)

    for item in items[:3]:
        href = item["href"]
        if "naukri.com" not in href:
            continue
        if "/mnjuser/" not in href and "/recruiterprofile/" not in href:
            continue

        out["profile_url"] = href
        try:
            await page.goto(href, wait_until="domcontentloaded", timeout=12000)
            await page.wait_for_timeout(1000)
            body  = await page.inner_text("body")
            email = _extract_email_from_text(body, company_domain)
            phone = _extract_phone_from_text(body)
            if email:
                out["email"] = email
            if phone:
                out["phone"] = phone
        except Exception as exc:
            logger.debug("naukri_profile_visit_failed", url=href, error=str(exc))

        break  # first matching result is enough

    return out


async def _ddg_hierarchy_search(page: Any, company_name: str) -> str:
    """
    Step 4: Find TA/HR leadership at company via DuckDuckGo.
    Returns pipe-separated 'Name (Title)' string.
    """
    query = f'"Head of Talent Acquisition" OR "CHRO" OR "TA Head" "{company_name}" site:linkedin.com/in/'
    items = await _ddg_search_raw(page, query, timeout_ms=10000)

    found:      list[str] = []
    seen_names: set[str]  = set()

    for item in items[:4]:
        if "linkedin.com/in/" not in item["href"]:
            continue
        title_clean = item["title"].replace("| LinkedIn", "").strip()
        name  = ""
        role  = ""
        if " - " in title_clean:
            parts = title_clean.split(" - ", 1)
            name  = parts[0].strip()
            role  = parts[1].strip()[:60]
        else:
            name = title_clean

        if name and name.lower() not in seen_names:
            seen_names.add(name.lower())
            found.append(name + (f" ({role})" if role else ""))

    return " | ".join(found) if found else ""


# ═══════════════════════════════════════════════════════════════════════════════
# ProspectIntelligenceAgent
# ═══════════════════════════════════════════════════════════════════════════════

class ProspectIntelligenceAgent:
    """
    Orchestrates recruiter contact discovery for prospects from an Excel file.

    Usage::

        agent  = ProspectIntelligenceAgent(concurrency=2)
        result = await agent.run("data/prospects/input/prospects.xlsx")
    """

    def __init__(self, concurrency: int = _DEFAULT_CONCURRENCY) -> None:
        """Set up a strictly sequential enricher; the requested concurrency is ignored (always 1).

        Also reads the min/max human-like delay between profile visits from settings.
        """
        # Force SEQUENTIAL profile visits regardless of the requested value —
        # parallel /in/ opens are a strong bot signal. The `concurrency` param is
        # kept for backward compatibility but is always clamped to 1.
        if concurrency and concurrency > 1:
            logger.info("prospect_concurrency_clamped", requested=concurrency, effective=1)
        self._concurrency = 1
        self._llm_service: Any = None
        # Human-like gap (ms) inserted between sequential prospect profile visits,
        # shared with the harvest recruiter pass (see _enrich_one below).
        from app.config import get_settings
        _s = get_settings()
        self._visit_delay_min_ms = int(getattr(_s, "linkedin_profile_visit_delay_min_ms", 8_000))
        self._visit_delay_max_ms = int(getattr(_s, "linkedin_profile_visit_delay_max_ms", 25_000))

    def _get_llm_service(self) -> Any:
        """Lazily build an LLMService from Settings — used as the authoritative
        email/phone extractor over the captured LinkedIn contact-info text (see
        _extract_linkedin_contact_info). Built once per agent, not per prospect."""
        if self._llm_service is None:
            from app.config import get_settings
            from app.services.llm_service import LLMService
            self._llm_service = LLMService(get_settings())
        return self._llm_service

    async def run(self, xlsx_path: str) -> ProspectIntelligenceResult:
        """Enrich every prospect in the Excel file with publicly scraped contact details.

        Profiles are visited one at a time in the persistent Chrome profile, with a random
        human-like pause between visits. Intermediate JSON is saved per batch; at the end the
        final JSON, debug JSON, Excel report and summary file are written. Returns an empty
        result when the sheet has no prospects; a missing file raises FileNotFoundError.
        Per-prospect failures become Low-confidence NOT_FOUND results.
        """
        started_at = datetime.now(timezone.utc)
        run_id     = started_at.strftime("%Y%m%d_%H%M%S")

        logger.info("prospect_intelligence_start", run_id=run_id, input=xlsx_path)

        prospects = _load_prospects(xlsx_path)
        if not prospects:
            completed_at = datetime.now(timezone.utc)
            return ProspectIntelligenceResult(
                run_id=run_id, started_at=started_at.isoformat(),
                completed_at=completed_at.isoformat(), runtime_minutes=0.0,
                total_prospects=0, enriched=0,
                high_confidence=0, medium_confidence=0, low_confidence=0,
                verified_emails=0, public_emails=0,
                verified_phones=0, public_phones=0, no_contact=0,
                json_path="", excel_path="",
            )

        all_results: list[ProspectResult] = []

        from app.services.config_service import ConfigService
        from app.scrapers.browser_manager import PersistentBrowserManager

        config = ConfigService().load()
        sem    = asyncio.Semaphore(self._concurrency)

        async def _enrich_one(prospect: ProspectRecord) -> ProspectResult:
            """Enrich one prospect in a fresh tab, then close it and sleep the anti-detection gap.

            The gap is taken while still holding the semaphore so the next visit waits for it.
            Errors are logged and turned into a Low-confidence NOT_FOUND result, never raised.
            """
            async with sem:
                page = await pbm.new_page()
                try:
                    return await self._enrich_prospect(page, prospect)
                except Exception as exc:
                    logger.warning("enrich_error", person=prospect.person_name, error=str(exc))
                    domain, website = _infer_company_domain(prospect.company_name)
                    return ProspectResult(
                        company_name     = prospect.company_name,
                        person_name      = prospect.person_name,
                        designation      = prospect.designation,
                        company_domain   = domain,
                        company_website  = website,
                        email_status     = "NOT_FOUND",
                        phone_status     = "NOT_FOUND",
                        department       = _infer_department(prospect.designation),
                        confidence_score = "Low",
                        source           = "Error",
                        enrichment_audit = [f"Exception: {exc}"],
                    )
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
                    # Human-like gap between sequential prospect profile visits,
                    # held while the (size-1) semaphore is still owned so the next
                    # prospect can't start until the pause elapses (anti-detection).
                    try:
                        await asyncio.sleep(
                            random.uniform(
                                self._visit_delay_min_ms / 1000.0,
                                self._visit_delay_max_ms / 1000.0,
                            )
                        )
                    except Exception:
                        pass

        async with PersistentBrowserManager(
            profile_dir = config.browser.chrome_profile,
            headless    = config.browser.headless,
            slow_mo     = config.browser.slow_mo_ms,
        ) as pbm:
            BATCH = _INTERMEDIATE_EVERY
            for batch_start in range(0, len(prospects), BATCH):
                batch         = prospects[batch_start: batch_start + BATCH]
                batch_tasks   = [_enrich_one(p) for p in batch]
                batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

                for i, r in enumerate(batch_results):
                    if isinstance(r, Exception):
                        p               = batch[i]
                        domain, website = _infer_company_domain(p.company_name)
                        all_results.append(ProspectResult(
                            company_name     = p.company_name,
                            person_name      = p.person_name,
                            designation      = p.designation,
                            company_domain   = domain,
                            company_website  = website,
                            email_status     = "NOT_FOUND",
                            phone_status     = "NOT_FOUND",
                            department       = _infer_department(p.designation),
                            confidence_score = "Low",
                            source           = "Error",
                            enrichment_audit = [f"gather exception: {r}"],
                        ))
                    else:
                        all_results.append(r)  # type: ignore[arg-type]

                batch_num = batch_start // BATCH + 1
                _save_intermediate(all_results, batch_num, run_id)
                logger.info(
                    "batch_complete",
                    batch=batch_num, batch_count=len(batch),
                    total_done=len(all_results), total=len(prospects),
                )

        json_path  = _save_final_json(all_results, run_id)
        debug_path = _save_debug_json(all_results, run_id)
        excel_path = ""
        try:
            from app.services.prospect_excel_service import ProspectExcelService
            excel_path = ProspectExcelService().export(all_results, run_id)
        except Exception as exc:
            logger.warning("excel_export_failed", error=str(exc))

        completed_at    = datetime.now(timezone.utc)
        elapsed_seconds = (completed_at - started_at).total_seconds()

        result = ProspectIntelligenceResult(
            run_id            = run_id,
            started_at        = started_at.isoformat(),
            completed_at      = completed_at.isoformat(),
            runtime_minutes   = round(elapsed_seconds / 60, 1),
            total_prospects   = len(prospects),
            enriched          = sum(1 for r in all_results if r.linkedin_url or r.company_domain),
            high_confidence   = sum(1 for r in all_results if r.confidence_score == "High"),
            medium_confidence = sum(1 for r in all_results if r.confidence_score == "Medium"),
            low_confidence    = sum(1 for r in all_results if r.confidence_score == "Low"),
            verified_emails   = sum(1 for r in all_results if r.email_status == "VERIFIED"),
            public_emails     = sum(1 for r in all_results if r.email_status == "PUBLIC"),
            verified_phones   = sum(1 for r in all_results if r.phone_status == "VERIFIED"),
            public_phones     = sum(1 for r in all_results if r.phone_status == "PUBLIC"),
            no_contact        = sum(
                1 for r in all_results
                if r.email_status == "NOT_FOUND" and r.phone_status == "NOT_FOUND"
            ),
            json_path  = json_path,
            excel_path = excel_path,
            results    = all_results,
        )

        _save_summary(run_id, result)

        logger.info(
            "prospect_intelligence_complete",
            run_id          = run_id,
            total           = result.total_prospects,
            verified_emails = result.verified_emails,
            public_emails   = result.public_emails,
            no_contact      = result.no_contact,
            runtime_minutes = result.runtime_minutes,
            debug_path      = debug_path,
        )
        return result

    # ── Per-record enrichment ──────────────────────────────────────────────────

    async def _enrich_prospect(self, page: Any, prospect: ProspectRecord) -> ProspectResult:
        """
        Full enrichment pipeline for one recruiter / prospect.

        Step 1 — Company website scraping   → VERIFIED
        Step 2 — LinkedIn profile visit     → PUBLIC
        Step 3 — Naukri cross-validation    → PUBLIC
        Step 4 — Hierarchy discovery
        Step 5 — Confidence scoring

        NO email or phone prediction. NOT_FOUND means not publicly available.
        """
        t0      = time.monotonic()
        result  = ProspectResult(
            company_name = prospect.company_name,
            person_name  = prospect.person_name,
            designation  = prospect.designation,
        )
        sources: list[str] = []
        audit:   list[str] = []

        logger.info("enriching", person=prospect.person_name, company=prospect.company_name)

        # ── Company domain ─────────────────────────────────────────────────────
        domain, website = _infer_company_domain(prospect.company_name)
        result.company_domain  = domain
        result.company_website = website

        # ══════════════════════════════════════════════════════════════════════
        # Step 1 — Company website scraping (VERIFIED)  [DISABLED — LLM-ONLY MODE]
        # ══════════════════════════════════════════════════════════════════════
        # Per requirement, contact info must come ONLY from the LLM call (Step 2).
        # This step uses web scraping + regex extraction and is intentionally
        # commented out.
        # if website:
        #     try:
        #         contact = await _scrape_company_website_contacts(
        #             page, website, prospect.person_name, domain
        #         )
        #         if contact["email"]:
        #             result.official_email_id = contact["email"]
        #             result.email_status      = "VERIFIED"
        #             sources.append("Company Website")
        #             audit.append(f"S1 VERIFIED email: {contact['email']} (page:{contact['source_page']})")
        #         else:
        #             audit.append(f"S1 website scraped — no personal email found (domain:{domain})")
        #         if contact["phone"]:
        #             result.contact_number = contact["phone"]
        #             result.phone_status   = "VERIFIED"
        #             sources.append("Company Website (Phone)")
        #             audit.append(f"S1 VERIFIED phone: {contact['phone']}")
        #     except Exception as exc:
        #         audit.append(f"S1 error: {exc}")

        # ══════════════════════════════════════════════════════════════════════
        # Step 2 — LinkedIn profile visit (PUBLIC)
        # ══════════════════════════════════════════════════════════════════════
        try:
            linkedin_url = prospect.existing_linkedin

            if not linkedin_url:
                ddg = await _ddg_linkedin_search(page, prospect.person_name, prospect.company_name)
                await page.wait_for_timeout(600)
                linkedin_url             = ddg.get("linkedin_url", "")
                result.linkedin_headline = ddg.get("headline", "")
                if linkedin_url:
                    sources.append("DuckDuckGo")
                    audit.append(f"S2 LinkedIn URL found via DDG: {linkedin_url}")
                else:
                    audit.append("S2 DDG LinkedIn search: no profile found")

            if linkedin_url:
                result.linkedin_url = linkedin_url
                contact_info = await _extract_linkedin_contact_info(
                    page, linkedin_url, domain, llm_service=self._get_llm_service(),
                )

                result.profile_opened        = contact_info["profile_opened"]
                result.contact_section_found = contact_info["contact_section_found"]

                if contact_info["headline"] and not result.linkedin_headline:
                    result.linkedin_headline = contact_info["headline"]
                if contact_info["location"]:
                    result.location = contact_info["location"]

                if contact_info["email"] and result.email_status != "VERIFIED":
                    result.official_email_id = contact_info["email"]
                    result.email_status      = "PUBLIC"
                    sources.append("LinkedIn Profile")
                    audit.append(f"S2 PUBLIC email from LinkedIn: {contact_info['email']}")
                else:
                    audit.append(
                        f"S2 LinkedIn visited — no email found "
                        f"(contact_section:{contact_info['contact_section_found']})"
                    )

                if contact_info["phone"] and result.phone_status != "VERIFIED":
                    result.contact_number = contact_info["phone"]
                    result.phone_status   = "PUBLIC"
                    sources.append("LinkedIn Profile (Phone)")
                    audit.append(f"S2 PUBLIC phone from LinkedIn: {contact_info['phone']}")

                # Extended metadata — scraped from the already-loaded profile page
                if result.profile_opened:
                    try:
                        meta = await _extract_linkedin_profile_metadata(page)
                        if meta["employment_type"] != "NOT_FOUND":
                            result.employment_type = meta["employment_type"]
                        if meta["years_in_company"] != "NOT_FOUND":
                            result.years_in_company = meta["years_in_company"]
                        if meta["overall_experience"] != "NOT_FOUND":
                            result.overall_experience = meta["overall_experience"]
                        if meta["company_industry"] != "NOT_FOUND":
                            result.company_industry = meta["company_industry"]
                        if meta["company_size"] != "NOT_FOUND":
                            result.company_size = meta["company_size"]
                        audit.append(
                            f"S2 metadata: emp_type={meta['employment_type']}, "
                            f"tenure={meta['years_in_company']}, "
                            f"industry={meta['company_industry']}, "
                            f"size={meta['company_size']}"
                        )
                    except Exception as exc:
                        audit.append(f"S2 metadata error: {exc}")

        except Exception as exc:
            audit.append(f"S2 error: {exc}")

        # ══════════════════════════════════════════════════════════════════════
        # Step 2.5 — Apollo.io (last resort) when LinkedIn yielded no email
        # ══════════════════════════════════════════════════════════════════════
        if not result.official_email_id and result.linkedin_url:
            try:
                from app.config import get_settings
                from app.core.dependencies import get_session_factory
                from app.services.apollo_enrichment import apollo_contact_fallback
                from app.services.recruiter_service import save_enrichment, upsert_recruiter

                settings = get_settings()
                session_factory = get_session_factory(settings)
                recruiter_id: str | None = None
                apollo_last = None
                # Warm the shared recruiter cache: resolve/create the row so the
                # Apollo recheck cooldown is honored across all three pipelines.
                try:
                    async with session_factory() as db:
                        recruiter = await upsert_recruiter(
                            db,
                            person_name=prospect.person_name,
                            company_name=prospect.company_name,
                            designation=prospect.designation or "",
                            linkedin_profile_url=result.linkedin_url,
                            harvest_source="ProspectIntelligence",
                        )
                        if recruiter is not None:
                            recruiter_id = recruiter.id
                            apollo_last = recruiter.apollo_enriched_at
                        await db.commit()
                except Exception as exc:
                    audit.append(f"S2.5 recruiter cache read error: {exc}")

                apollo = await apollo_contact_fallback(
                    settings=settings,
                    linkedin_url=result.linkedin_url,
                    person_name=prospect.person_name,
                    company_name=prospect.company_name,
                    company_domain=domain,
                    already_email=result.email_status in ("VERIFIED", "PUBLIC"),
                    already_phone=result.phone_status in ("VERIFIED", "PUBLIC"),
                    apollo_enriched_at=apollo_last,
                )
                if apollo.email:
                    result.official_email_id = apollo.email
                    result.email_status      = "VERIFIED"
                    sources.append("Contact Database")
                    audit.append(f"S2.5 VERIFIED email from contact enrichment: {apollo.email}")
                if apollo.phone and result.phone_status != "VERIFIED":
                    result.contact_number = apollo.phone
                    result.phone_status   = "PUBLIC"
                    sources.append("Contact Database (Phone)")
                    audit.append(f"S2.5 phone from contact enrichment: {apollo.phone}")
                if apollo.attempted and not apollo.email:
                    audit.append("S2.5 contact enrichment: no email found")

                # Persist Apollo's result + attempt stamp back to the shared cache.
                if recruiter_id and apollo.attempted:
                    try:
                        async with session_factory() as db:
                            await save_enrichment(
                                db, recruiter_id,
                                official_email_id    = apollo.email or "",
                                email_status         = "VERIFIED" if apollo.email else "NOT_FOUND",
                                contact_number       = apollo.phone or "",
                                phone_status         = "PUBLIC" if apollo.phone else "NOT_FOUND",
                                secondary_email      = apollo.secondary_email,
                                company_linkedin_url = apollo.company_linkedin_url,
                                address              = apollo.address,
                                city                 = apollo.city,
                                state                = apollo.state,
                                country              = apollo.country,
                                enrichment_source    = "apollo" if apollo.source else "",
                                apollo_attempted     = True,
                                verified             = bool(apollo.email or apollo.phone),
                                apollo_person_id     = apollo.person_id,
                            )
                            await db.commit()
                    except Exception as exc:
                        audit.append(f"S2.5 enrichment cache write error: {exc}")
            except Exception as exc:
                audit.append(f"S2.5 contact enrichment error: {exc}")

        # ══════════════════════════════════════════════════════════════════════
        # Step 3 — Naukri cross-source (PUBLIC)  [DISABLED — LLM-ONLY MODE]
        # ══════════════════════════════════════════════════════════════════════
        # Per requirement, contact info must come ONLY from the LLM call (Step 2).
        # This step uses DuckDuckGo web scraping + regex extraction and is
        # intentionally commented out.
        # if result.email_status == "NOT_FOUND" or result.phone_status == "NOT_FOUND":
        #     try:
        #         naukri = await _search_naukri_contact(
        #             page, prospect.person_name, prospect.company_name, domain
        #         )
        #         if naukri["profile_url"]:
        #             sources.append("Naukri")
        #             audit.append(f"S3 Naukri profile found: {naukri['profile_url']}")
        #         if naukri["email"] and result.email_status == "NOT_FOUND":
        #             result.official_email_id = naukri["email"]
        #             result.email_status      = "PUBLIC"
        #             audit.append(f"S3 PUBLIC email from Naukri: {naukri['email']}")
        #         if naukri["phone"] and result.phone_status == "NOT_FOUND":
        #             result.contact_number = naukri["phone"]
        #             result.phone_status   = "PUBLIC"
        #             audit.append(f"S3 PUBLIC phone from Naukri: {naukri['phone']}")
        #         if not naukri["profile_url"]:
        #             audit.append("S3 Naukri: no profile found")
        #     except Exception as exc:
        #         audit.append(f"S3 Naukri error: {exc}")

        # ── Update email_found / phone_found diagnostics ───────────────────────
        result.email_found = result.email_status in ("VERIFIED", "PUBLIC")
        result.phone_found = result.phone_status in ("VERIFIED", "PUBLIC")

        # ══════════════════════════════════════════════════════════════════════
        # Step 4 — Hierarchy discovery (DuckDuckGo)
        # ══════════════════════════════════════════════════════════════════════
        if prospect.company_name:
            try:
                hierarchy = await _ddg_hierarchy_search(page, prospect.company_name)
                result.reporting_hierarchy = hierarchy
                result.hierarchy_found     = bool(hierarchy)
                if hierarchy:
                    sources.append("DDG Hierarchy")
                    audit.append(f"S4 Hierarchy: {hierarchy[:100]}")
                else:
                    audit.append("S4 Hierarchy: none found")
            except Exception as exc:
                audit.append(f"S4 hierarchy error: {exc}")

        # ── Department inference ────────────────────────────────────────────────
        result.department = _infer_department(prospect.designation, result.linkedin_headline)

        # ── Position level & hiring domain (deterministic classifiers) ──────────
        result.position_level = _classify_position_level(prospect.designation, result.linkedin_headline)
        result.hiring_domain  = _classify_hiring_domain(prospect.designation, result.linkedin_headline, [])

        # ── Confidence scoring ─────────────────────────────────────────────────
        result.confidence_score = _score_confidence(
            result, prospect.company_name, prospect.designation
        )
        result.source           = ", ".join(dict.fromkeys(sources))
        result.enrichment_audit = audit

        elapsed = round(time.monotonic() - t0, 1)
        logger.info(
            "enriched",
            person       = prospect.person_name,
            linkedin     = bool(result.linkedin_url),
            email_status = result.email_status,
            phone_status = result.phone_status,
            confidence   = result.confidence_score,
            duration_s   = elapsed,
        )
        return result
