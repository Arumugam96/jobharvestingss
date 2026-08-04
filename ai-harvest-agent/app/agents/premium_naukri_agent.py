"""
Premium Naukri Agent — searches Naukri recruiter profiles and extracts contact details.

What this agent does
────────────────────
• Navigates to Naukri using the existing persistent Chrome session (Premium account).
• Searches for a recruiter by name + current company + previous companies (if known).
• Opens the recruiter profile page.
• Extracts: official email, contact number, designation, employment history, location.

Search strategy (3 levels)
───────────────────────────
Level 1 — Direct Naukri profile search:
    https://www.naukri.com/mnjuser/profile?id=&altresid=
    Searches internal Naukri candidate/recruiter database.

Level 2 — DuckDuckGo-assisted discovery:
    site:naukri.com/mnjuser "{recruiter_name}" "{company}"
    → Navigate to discovered profile URL.

Level 3 — Naukri recruiter homepage search:
    https://www.naukri.com/recruiters/{slug}

Security contract (same as all agents)
───────────────────────────────────────
• Uses persistent Chrome profile — user is already logged into Naukri Premium.
• DO NOT implement or call any login / OTP / MFA flow.
• DO NOT fabricate email or phone numbers.
• Only data scraped from an actual Naukri profile page is stored.
• email_status / phone_status: VERIFIED (company domain match) | PUBLIC | NOT_FOUND.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

import structlog

from app.models.lead_models import NaukriProfile

logger = structlog.get_logger(__name__)

# ── Regex patterns ─────────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(
    r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'
)
_PHONE_RE = re.compile(
    r'(?:(?:\+91|0091|91)?[\s\-]?[6-9]\d{9}|(?:\+[1-9]\d{6,14}))'
)

# ── Naukri profile extraction JS (robust multi-selector) ─────────────────────

_NAUKRI_PROFILE_JS = """() => {
    function text(selectors) {
        for (const s of selectors) {
            const el = document.querySelector(s);
            if (el && el.textContent.trim()) return el.textContent.trim();
        }
        return '';
    }

    const name = text([
        '.name-container .name', '.nameText', 'h1.bold',
        '.candidate-name h1', '.candidate-detail h1',
        '[class*="name"] h1', '[class*="candidateName"]',
        'h1[class*="name"]', '.profileName',
    ]);

    const designation = text([
        '.designation', '.current-designation',
        '.profile-headline .desig', '.candidate-headline',
        '[class*="designation"]', '[class*="currentDesig"]',
        '.currentDesignation', '.headline',
    ]);

    const company = text([
        '.current-company', '.currComp',
        '.profile-headline .comp', '.company-name',
        '[class*="currentCompany"]', '[class*="company"]',
        '.companyName', '.currCompany',
    ]);

    const location = text([
        '.location-label', '.loc', '.candidate-location', '.city',
        '[class*="location"]', '[class*="city"]',
        '.candidateLocation', '.profileLocation',
    ]);

    // Email — Naukri premium shows email in contact section or modal
    let email = '';
    const emailSelectors = [
        '.email-value', '.mail-id', '[data-label="Email"]',
        '.contact-email', 'a[href^="mailto:"]',
        '[class*="email"]', '[class*="mailId"]',
        '.candidateEmail', '.contactEmail',
        'span[id*="email"]', 'div[id*="email"]',
    ];
    for (const sel of emailSelectors) {
        const els = document.querySelectorAll(sel);
        for (const el of els) {
            const val = el.textContent.trim() || el.getAttribute('href')?.replace('mailto:', '') || '';
            if (val && /[^@\\s]+@[^@\\s]+\\.[^@\\s]+/.test(val)) { email = val.trim(); break; }
        }
        if (email) break;
    }

    // Phone — Naukri premium shows phone in contact section or modal
    let phone = '';
    const phoneSelectors = [
        '.phone-value', '.mobile-no', '[data-label="Mobile"]',
        '.contact-phone', '[class*="phone"]', '[class*="mobile"]',
        '.candidatePhone', '.contactPhone', '.mobileNo',
        'span[id*="phone"]', 'div[id*="mobile"]',
    ];
    for (const sel of phoneSelectors) {
        const els = document.querySelectorAll(sel);
        for (const el of els) {
            const val = el.textContent.trim().replace(/[\\s\\-\\(\\)]/g, '');
            if (val && /^(?:\\+91|0091|91)?[6-9]\\d{9}$/.test(val)) {
                phone = val;
                break;
            }
        }
        if (phone) break;
    }

    // Employment history
    const expItems = Array.from(document.querySelectorAll(
        '.exp-container .exp-item, .experience-section .experience-item, ' +
        '.work-exp-section .companyInfo, .workExperienceSection .designation-name, ' +
        '[class*="experience"] [class*="company"], [class*="workExp"] [class*="comp"]'
    )).map(el => el.textContent.trim().replace(/\\s+/g, ' ')).filter(Boolean);

    // LinkedIn URL (if Naukri profile links it)
    let linkedinUrl = '';
    const liLink = document.querySelector(
        'a[href*="linkedin.com/in/"], a[title*="LinkedIn"], a[data-label="LinkedIn"],' +
        'a[href*="linkedin"]'
    );
    if (liLink) {
        linkedinUrl = liLink.href || liLink.getAttribute('href') || '';
    }

    return {
        recruiter_name:    name,
        designation:       designation,
        current_company:   company,
        location:          location,
        email:             email,
        phone:             phone,
        employment_history: expItems.slice(0, 10),
        linkedin_url:      linkedinUrl,
    };
}"""


# ══════════════════════════════════════════════════════════════════════════════
# Premium Naukri Agent
# ══════════════════════════════════════════════════════════════════════════════

class PremiumNaukriAgent:
    """
    Searches Naukri recruiter profiles for contact details.

    Accepts a Playwright page that already has an active Naukri Premium session
    (loaded via the persistent Chrome profile). Does NOT handle any login.
    """

    def __init__(self, max_profiles: int = 3) -> None:
        self._max_profiles = max_profiles
        self._session_ok: bool | None = None  # cached per-agent-instance to avoid re-verifying

    # ── Session verification ───────────────────────────────────────────────────

    async def _verify_naukri_session(self, page: Any) -> bool:
        """
        Verify the Chrome profile has an active Naukri Premium session.

        Navigates to recruit.naukri.com. If redirected to a login page, the
        session is missing — caller should skip enrichment and log a hint
        to run POST /naukri-setup-session.
        """
        _LOGIN_PATHS = ("/login", "/nlogin", "accounts.naukri.com", "/recruit/login")
        try:
            await page.goto(
                "https://recruit.naukri.com/",
                wait_until="domcontentloaded",
                timeout=20_000,
            )
            await page.wait_for_timeout(1_500)
            current_url = page.url
            if any(p in current_url for p in _LOGIN_PATHS):
                logger.warning(
                    "naukri_not_authenticated",
                    url  = current_url,
                    hint = "Run POST /naukri-setup-session to log in and save the session",
                )
                return False
            has_login_form = await page.evaluate("""() => {
                return !!(
                    document.querySelector('input[type="password"][name]') ||
                    document.querySelector('form#loginform') ||
                    document.querySelector('.loginPage') ||
                    document.querySelector('[class*="loginForm"]')
                );
            }""")
            if has_login_form:
                logger.warning(
                    "naukri_login_page_detected",
                    url  = current_url,
                    hint = "Run POST /naukri-setup-session to log in and save the session",
                )
                return False
            logger.info("naukri_session_active", url=current_url)
            return True
        except Exception as exc:
            logger.warning("naukri_session_check_failed", error=str(exc))
            return False

    # ── Public entry point ─────────────────────────────────────────────────────

    async def search_recruiter(
        self,
        page:               Any,
        recruiter_name:     str,
        current_company:    str = "",
        previous_companies: list[str] | None = None,
    ) -> NaukriProfile | None:
        """
        Find a recruiter's Naukri profile and extract contact details.

        Verifies Naukri session first. Tries two search strategies:
          1. DuckDuckGo site search → navigate to discovered Naukri profile URL
          2. Naukri internal search via recruit.naukri.com / mnjuser

        Returns a NaukriProfile if found, None if session missing or no profile found.
        """
        logger.info(
            "fallback_to_premium_naukri",
            recruiter  = recruiter_name,
            company    = current_company,
        )

        # Gate on active session — verified once per agent instance to avoid
        # navigating to recruit.naukri.com before every single record lookup.
        if self._session_ok is None:
            self._session_ok = await self._verify_naukri_session(page)
        if not self._session_ok:
            logger.warning(
                "premium_naukri_skipped_no_session",
                recruiter = recruiter_name,
                hint      = "POST /naukri-setup-session to authenticate the Chrome profile first",
            )
            return None

        companies = [current_company] + (previous_companies or [])
        companies = [c for c in companies if c]

        # Strategy 1: DuckDuckGo search → navigate to Naukri profile URL
        profile = await self._search_via_duckduckgo(page, recruiter_name, companies)
        if profile and (profile.email or profile.phone):
            return profile

        # Strategy 2: Naukri internal recruiter search
        if not profile:
            profile = await self._search_naukri_internal(page, recruiter_name, current_company)

        return profile

    # ── Strategy 1 — DuckDuckGo ───────────────────────────────────────────────

    async def _search_via_duckduckgo(
        self,
        page:      Any,
        name:      str,
        companies: list[str],
    ) -> NaukriProfile | None:
        """
        Use DuckDuckGo to find the recruiter's naukri.com/mnjuser profile URL,
        then navigate to it and extract data.
        """
        for company in companies[:2]:
            query = f'site:naukri.com/mnjuser "{name}" "{company}"'
            ddg_url = (
                "https://duckduckgo.com/?q="
                + urllib.parse.quote_plus(query)
                + "&ia=web"
            )
            logger.info(
                "premium_naukri_ddg_search",
                query     = query,
                recruiter = name,
                company   = company,
            )

            try:
                await page.goto(ddg_url, wait_until="domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(2_000)

                naukri_url = await self._extract_naukri_link_from_ddg(page)
                if naukri_url:
                    profile = await self._extract_profile(page, naukri_url, name)
                    if profile:
                        return profile
            except Exception as exc:
                logger.debug("premium_naukri_ddg_failed", error=str(exc))

        return None

    async def _extract_naukri_link_from_ddg(self, page: Any) -> str:
        """Extract the first Naukri profile URL from DuckDuckGo results."""
        try:
            links = await page.evaluate("""() => {
                const results = document.querySelectorAll(
                    'a[data-testid="result-title-a"], .result__a, article a, h2 a'
                );
                return Array.from(results)
                    .map(a => a.href || a.getAttribute('href') || '')
                    .filter(h =>
                        h.includes('naukri.com/mnjuser') ||
                        h.includes('naukri.com/recruiter') ||
                        h.includes('naukri.com/profile') ||
                        (h.includes('naukri.com/') &&
                         !h.includes('naukri.com/jobs') &&
                         !h.includes('naukri.com/job-listings') &&
                         !h.includes('naukri.com/company'))
                    );
            }""")
            return links[0] if links else ""
        except Exception:
            return ""

    # ── Strategy 2 — Naukri internal recruiter search ─────────────────────────

    async def _search_naukri_internal(
        self,
        page:    Any,
        name:    str,
        company: str,
    ) -> NaukriProfile | None:
        """
        Search Naukri for a recruiter profile using the authenticated session.
        Tries recruit.naukri.com dashboard search, then mnjuser resdex fallback.
        """
        _LOGIN_PATHS = ("/login", "/nlogin", "accounts.naukri.com")
        search_query = urllib.parse.quote_plus(f"{name} {company}".strip())

        # Ordered search URLs: recruit.naukri.com first (user's correct portal)
        candidate_urls = [
            f"https://www.naukri.com/mnjuser/homepage?search={search_query}",
            f"https://recruit.naukri.com/searchCV?keywords={search_query}",
        ]

        for search_url in candidate_urls:
            try:
                logger.info(
                    "premium_naukri_internal_search",
                    name=name, company=company, url=search_url,
                )
                await page.goto(search_url, wait_until="domcontentloaded", timeout=25_000)
                await page.wait_for_timeout(2_500)

                # Skip if redirected to login
                if any(p in page.url for p in _LOGIN_PATHS):
                    logger.debug("premium_naukri_search_redirected_to_login", url=page.url)
                    continue

                # Find the first matching profile link
                profile_url = await page.evaluate("""(searchName) => {
                    const links = document.querySelectorAll(
                        'a[href*="/mnjuser/"], a[href*="/profile"], ' +
                        '.profile-card a, .candidate-name a, ' +
                        'a.candidateName, [class*="candidateName"] a, ' +
                        '[class*="profileName"] a'
                    );
                    const firstName = searchName.toLowerCase().split(' ')[0];
                    for (const a of links) {
                        const t = (a.textContent || '').toLowerCase();
                        if (t.includes(firstName)) {
                            return a.href || a.getAttribute('href') || '';
                        }
                    }
                    return links[0]?.href || links[0]?.getAttribute('href') || '';
                }""", name)

                if profile_url and "naukri.com" in profile_url:
                    return await self._extract_profile(page, profile_url, name)

            except Exception as exc:
                logger.debug(
                    "premium_naukri_internal_search_failed",
                    url=search_url, error=str(exc),
                )

        return None

    # ── Profile extraction ─────────────────────────────────────────────────────

    async def _extract_profile(
        self,
        page:        Any,
        profile_url: str,
        expected_name: str,
    ) -> NaukriProfile | None:
        """Navigate to profile_url and extract recruiter data."""
        try:
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=25_000)
            await page.wait_for_timeout(2_000)

            # Naukri Premium hides contact details behind a "View Contact" button.
            # Click it first so email/phone appear in the DOM before extraction.
            _REVEAL_SELECTORS = [
                'button:has-text("View Contact")',
                'button:has-text("Get Contact")',
                'button:has-text("View Phone")',
                'button:has-text("Contact Candidate")',
                '[data-ga-category="get_contact"]',
                '[class*="viewContact"]',
                '[class*="getContact"]',
                '.view-contact-btn',
                '.getContactBtn',
            ]
            for sel in _REVEAL_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0:
                        await btn.click(timeout=5_000)
                        await page.wait_for_timeout(2_000)
                        logger.info("naukri_contact_reveal_clicked", selector=sel)
                        break
                except Exception:
                    continue

            data = await page.evaluate(_NAUKRI_PROFILE_JS)

            found_name = data.get("recruiter_name", "").strip()
            email      = data.get("email", "").strip()
            phone      = data.get("phone", "").strip()

            # Fallback: scan full page text for email / phone if extraction JS missed them
            if not email or not phone:
                page_text = await page.evaluate("() => document.body.innerText || ''")
                if not email:
                    m = _EMAIL_RE.search(page_text)
                    email = m.group(0) if m else ""
                if not phone:
                    m = _PHONE_RE.search(page_text)
                    if m:
                        phone = re.sub(r"[\s\-]", "", m.group(0))

            if email:
                logger.info(
                    "email_found",
                    source     = "Premium Naukri",
                    recruiter  = found_name or expected_name,
                    email      = email,
                )
            if phone:
                logger.info(
                    "phone_found",
                    source     = "Premium Naukri",
                    recruiter  = found_name or expected_name,
                    phone      = phone,
                )

            if email or phone or found_name:
                logger.info(
                    "premium_profile_found",
                    url        = profile_url,
                    recruiter  = found_name or expected_name,
                    has_email  = bool(email),
                    has_phone  = bool(phone),
                )
                return NaukriProfile(
                    profile_url       = profile_url,
                    recruiter_name    = found_name or expected_name,
                    designation       = data.get("designation", ""),
                    current_company   = data.get("current_company", ""),
                    location          = data.get("location", ""),
                    email             = email,
                    phone             = phone,
                    employment_history= data.get("employment_history", []),
                    linkedin_url      = data.get("linkedin_url", ""),
                )

        except Exception as exc:
            logger.debug(
                "premium_naukri_profile_extract_failed",
                url   = profile_url,
                error = str(exc),
            )

        return None

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _classify_email(email: str, company: str) -> str:
        """
        Classify email status.
        VERIFIED if email domain matches company name (heuristic).
        PUBLIC if the email was found on the profile but domain doesn't match.
        """
        if not email:
            return "NOT_FOUND"
        domain = email.split("@", 1)[-1].lower()
        company_slug = re.sub(r"[^a-z0-9]", "", company.lower())
        # Check if company name appears in email domain (e.g. infosys in infosys.com)
        if len(company_slug) > 3 and company_slug[:6] in domain:
            return "VERIFIED"
        return "PUBLIC"

    @staticmethod
    def _normalize_phone(raw: str) -> str:
        """Strip spaces/hyphens from a phone string."""
        return re.sub(r"[\s\-\(\)]", "", raw)
