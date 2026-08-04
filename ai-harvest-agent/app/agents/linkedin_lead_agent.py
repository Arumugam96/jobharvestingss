"""
LinkedIn Lead Agent — discovers recruiter hiring posts on LinkedIn.

What this agent does
────────────────────
• Searches LinkedIn POSTS (NOT Jobs) for hiring keywords.
• Extracts post author (recruiter), headline, company, post URL.
• Extracts email / phone ONLY if explicitly shared in the post text.
• Navigates to the recruiter's LinkedIn profile to get more detail.

Security contract (identical to all other agents in this repo)
───────────────────────────────────────────────────────────────
• Uses the persistent Chrome profile at data/chrome_profile/.
• User is already logged in to LinkedIn manually.
• DO NOT implement or call any login / OTP / MFA flow.
• DO NOT fabricate any email or phone number.
• email_status / phone_status: PUBLIC (post-shared) or NOT_FOUND only.
• Contact is only stored if scraped verbatim from a public post.

Page flow
─────────
1. Navigate to LinkedIn content search with the given keyword.
2. Dismiss any overlay / consent / modal.
3. Scroll to load more posts.
4. For each post, extract author + post metadata using JS evaluation.
5. Regex-scan post text for any explicitly shared email or phone.
6. Optionally visit recruiter's profile for headline / company / location.
7. Return list[LinkedInPost].
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

import structlog

from app.models.lead_models import LinkedInPost

logger = structlog.get_logger(__name__)

# ── Regex patterns for contact extraction from post text ─────────────────────
# Only matches email/phone that the recruiter explicitly typed in their post.
_EMAIL_RE = re.compile(
    r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'
)
_PHONE_RE = re.compile(
    r'(?:'
    r'(?:\+91|0091|91)?[\s\-]?[6-9]\d{9}'   # India mobile (10 digits, starts 6-9)
    r'|(?:\+[1-9]\d{6,14})'                   # International E.164
    r')'
)

# Hiring-intent keywords used to filter posts that are actually job posts
_HIRING_KEYWORDS = {
    "hiring", "we are hiring", "we're hiring", "looking for",
    "open position", "open role", "job opening", "vacancy",
    "join our team", "join us", "career opportunity", "immediate joiner",
    "urgently hiring", "talent acquisition", "recruitment",
}


def _is_hiring_post(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _HIRING_KEYWORDS)


def _extract_email_from_text(text: str) -> str:
    """Return the first email found in post text, or empty string."""
    m = _EMAIL_RE.search(text)
    return m.group(0) if m else ""


def _extract_phone_from_text(text: str) -> str:
    """Return the first phone found in post text, or empty string."""
    m = _PHONE_RE.search(text)
    if m:
        raw = m.group(0).strip()
        # Normalise whitespace / hyphens
        return re.sub(r'[\s\-]+', '', raw)
    return ""


def _parse_company_from_headline(headline: str) -> tuple[str, str]:
    """
    Parse 'Designation at Company' or 'Designation | Company' from a LinkedIn headline.
    Returns (designation, company).
    """
    for sep in (" at ", " @ ", " | ", " - "):
        if sep in headline:
            parts = headline.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    return headline.strip(), ""


def _normalize_linkedin_url(url: str) -> str:
    """Strip query params from LinkedIn profile URLs for stable dedup keys."""
    if not url:
        return ""
    try:
        p = urllib.parse.urlparse(url)
        return urllib.parse.urlunparse(p._replace(query="", fragment="")).rstrip("/")
    except Exception:
        return url


# ══════════════════════════════════════════════════════════════════════════════
# LinkedIn Lead Agent
# ══════════════════════════════════════════════════════════════════════════════

class LinkedInLeadAgent:
    """
    Searches LinkedIn POST results for recruiter hiring activity.

    Accepts a Playwright page that is already navigated to the correct
    LinkedIn session (persistent Chrome profile). Does NOT open a new browser.
    """

    def __init__(
        self,
        max_posts:    int = 50,
        max_pages:    int = 5,
        scroll_times: int = 8,
    ) -> None:
        self._max_posts    = max_posts
        self._max_pages    = max_pages
        self._scroll_times = scroll_times

    # ── Public entry point ─────────────────────────────────────────────────────

    async def search_posts(
        self,
        page:    Any,
        keyword: str,
    ) -> list[LinkedInPost]:
        """
        Search LinkedIn POSTS for `keyword`, extract recruiter data.
        Returns list[LinkedInPost].
        """
        logger.info("linkedin_search_started", keyword=keyword, max_posts=self._max_posts)

        all_posts:    list[LinkedInPost] = []
        seen_profiles: set[str]          = set()

        for page_num in range(self._max_pages):
            url = self._build_search_url(keyword, page_num)
            logger.info("linkedin_page_navigate", page_num=page_num, url=url)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(3_000)
                await self._dismiss_overlays(page)
                await self._scroll_to_load(page)
            except Exception as exc:
                logger.warning("linkedin_page_load_failed", page_num=page_num, error=str(exc))
                break

            # Diagnose: log actual URL and page title to detect login redirects
            try:
                actual_url   = page.url
                page_title   = await page.title()
                is_auth_wall = any(p in actual_url for p in ("/login", "/authwall", "/checkpoint", "/uas/"))
                logger.info("linkedin_page_landed", page_num=page_num, actual_url=actual_url[:80], title=page_title[:60], auth_wall=is_auth_wall)
            except Exception:
                pass

            # Container-count diagnostic — written to file so it survives across the API boundary
            try:
                _diag = await page.evaluate("""() => {
                    var r = {};
                    r.url = location.href.slice(0, 120);
                    r.title = document.title.slice(0, 80);
                    r.listitem   = document.querySelectorAll('div[role="listitem"]').length;
                    r.li_rsc     = document.querySelectorAll('li.reusable-search__result-container').length;
                    r.data_urn   = document.querySelectorAll('[data-urn]').length;
                    r.data_eurn  = document.querySelectorAll('[data-entity-urn]').length;
                    r.feed_v2    = document.querySelectorAll('.feed-shared-update-v2').length;
                    r.article    = document.querySelectorAll('article').length;
                    r.in_links   = document.querySelectorAll('a[href*="/in/"]').length;
                    r.span_dir   = document.querySelectorAll('span[dir]').length;
                    var sample = document.querySelector('div[role="listitem"]');
                    r.sample_text = sample ? sample.textContent.slice(0, 200) : '';
                    return r;
                }""")
                import json as _json, pathlib as _pl
                _pl.Path("data/results/lead_intelligence").mkdir(parents=True, exist_ok=True)
                _pl.Path("data/results/lead_intelligence/li_diag.json").write_text(
                    _json.dumps(_diag, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                logger.info("linkedin_container_diagnostic", page_num=page_num, **_diag)
            except Exception as _e:
                logger.warning("linkedin_diag_failed", error=str(_e))
            raw_posts = await self._extract_posts_from_page(page)
            logger.info("linkedin_page_extracted", page_num=page_num, count=len(raw_posts))

            for raw in raw_posts:
                if len(all_posts) >= self._max_posts:
                    break

                profile_url = _normalize_linkedin_url(raw.get("author_profile_url", ""))
                if not raw.get("author_name") or not profile_url:
                    continue
                if profile_url in seen_profiles:
                    continue
                seen_profiles.add(profile_url)

                post_text = raw.get("post_content", "")
                if not _is_hiring_post(post_text):
                    continue

                headline   = raw.get("author_headline", "")
                designation, company = _parse_company_from_headline(headline)

                # Prefer mailto: email surfaced directly from DOM; fall back to text extraction
                mailto_email = raw.get("raw_email", "").strip()
                post = LinkedInPost(
                    post_url           = raw.get("post_url", ""),
                    author_name        = raw.get("author_name", "").strip(),
                    author_profile_url = profile_url,
                    author_headline    = headline,
                    author_company     = company or raw.get("author_company", ""),
                    post_content       = post_text,
                    post_date          = raw.get("post_date", ""),
                    raw_email          = mailto_email or _extract_email_from_text(post_text),
                    raw_phone          = _extract_phone_from_text(post_text),
                )

                logger.info(
                    "linkedin_post_found",
                    author        = post.author_name,
                    company       = post.author_company,
                    has_email     = bool(post.raw_email),
                    has_phone     = bool(post.raw_phone),
                    profile_url   = profile_url,
                )
                all_posts.append(post)

            if len(all_posts) >= self._max_posts:
                break

        logger.info(
            "linkedin_search_completed",
            keyword         = keyword,
            posts_found     = len(all_posts),
            with_email      = sum(1 for p in all_posts if p.raw_email),
            with_phone      = sum(1 for p in all_posts if p.raw_phone),
        )
        return all_posts

    # ── Profile enrichment (optional second pass) ───────────────────────────────

    async def enrich_profile(self, page: Any, post: LinkedInPost) -> LinkedInPost:
        """
        Visit the recruiter's LinkedIn profile page to get fuller details.
        Updates post.author_company and post.author_headline in-place.
        Does NOT try to extract contact info — LinkedIn hides it behind auth.
        """
        if not post.author_profile_url:
            return post
        try:
            await page.goto(
                post.author_profile_url, wait_until="domcontentloaded", timeout=20_000
            )
            await page.wait_for_timeout(2_500)

            data = await page.evaluate("""() => {
                const headline = document.querySelector(
                    '.text-body-medium.break-words, .pv-text-details__left-panel .text-body-medium'
                )?.textContent?.trim() || '';

                const location = document.querySelector(
                    '.text-body-small.inline.t-black--light.break-words'
                )?.textContent?.trim() || '';

                const company = document.querySelector(
                    '[data-field="experience_company_logo"] .t-bold span[aria-hidden="true"]'
                )?.textContent?.trim() || '';

                return { headline, location, company };
            }""")

            if data.get("headline") and not post.author_headline:
                post.author_headline = data["headline"]
                designation, company = _parse_company_from_headline(data["headline"])
                if company and not post.author_company:
                    post.author_company = company

        except Exception as exc:
            logger.debug(
                "linkedin_profile_enrich_skipped",
                url=post.author_profile_url,
                reason=str(exc),
            )
        return post

    # ── Internals ─────────────────────────────────────────────────────────────

    def _build_search_url(self, keyword: str, page_num: int) -> str:
        """Build LinkedIn content search URL for posts."""
        params = {
            "keywords": keyword,
            "origin":   "GLOBAL_SEARCH_HEADER",
            "sid":      "abc",
        }
        if page_num > 0:
            params["start"] = str(page_num * 10)
        base = "https://www.linkedin.com/search/results/content/"
        return base + "?" + urllib.parse.urlencode(params)

    async def _dismiss_overlays(self, page: Any) -> None:
        """Dismiss any modal / cookie / consent overlays."""
        dismiss_selectors = [
            "button[data-control-name='overlay.dismiss_accept_policy']",
            "button[aria-label='Dismiss']",
            "button.msg-overlay-bubble-header__control--close",
            "#artdeco-modal-outlet button[data-tracking-control-name='overlay.dismiss']",
        ]
        for sel in dismiss_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1_000):
                    await btn.click()
                    await page.wait_for_timeout(500)
            except Exception:
                pass

    async def _scroll_to_load(self, page: Any) -> None:
        """Scroll down to trigger lazy-loaded posts."""
        for _ in range(self._scroll_times):
            try:
                await page.evaluate("window.scrollBy(0, window.innerHeight * 1.5)")
                await page.wait_for_timeout(800)
            except Exception:
                break

    async def _extract_posts_from_page(self, page: Any) -> list[dict]:
        """
        JavaScript evaluation to extract post data from the LinkedIn search results page.

        Uses a layered selector strategy to handle LinkedIn's frequently-changing DOM.
        Returns a list of raw dicts (pre-model).
        """
        try:
            return await page.evaluate("""() => {
                var posts = [];

                // LinkedIn 2026 DOM: posts use div[role='listitem'] as containers.
                // Legacy class-based selectors (.feed-shared-update-v2, etc.) are included
                // as fallbacks for any remaining LinkedIn DOM variant.
                var containers = Array.from(document.querySelectorAll(
                    'div[role="listitem"], ' +
                    'li.reusable-search__result-container, ' +
                    'div.feed-shared-update-v2, ' +
                    'div[data-urn], ' +
                    '.occludable-update'
                ));

                // Deduplicate containers by their profile URL to avoid double-counting
                var seenContainers = new Set();

                for (var ci = 0; ci < containers.length; ci++) {
                    var el = containers[ci];
                    try {
                        // ── Author profile URL ────────────────────────────────
                        // Find ALL /in/ links in the container, pick the one with the most text
                        var inLinks = Array.from(el.querySelectorAll('a[href*="/in/"]'));
                        var profileLinkEl = null;
                        for (var li2 = 0; li2 < inLinks.length; li2++) {
                            var t = (inLinks[li2].textContent || '').trim();
                            if (t.length > 2) { profileLinkEl = inLinks[li2]; break; }
                        }
                        if (!profileLinkEl && inLinks.length > 0) { profileLinkEl = inLinks[0]; }

                        var profileUrl = profileLinkEl
                            ? (profileLinkEl.href || profileLinkEl.getAttribute('href') || '')
                            : '';

                        // Skip if already processed this profile
                        var profileKey = profileUrl.split('?')[0];
                        if (profileKey && seenContainers.has(profileKey)) continue;
                        if (profileKey) seenContainers.add(profileKey);

                        // ── Author name ───────────────────────────────────────
                        // LinkedIn 2026: name is in the text of the /in/ link, may include
                        // degree indicator (· 2nd, · 3rd+). Strip those.
                        var authorName = '';
                        if (profileLinkEl) {
                            var rawName = (profileLinkEl.textContent || '').trim();
                            // Remove degree indicators and extra whitespace
                            authorName = rawName
                                .replace(/[\\u00b7\\u2022].*$/, '')  // strip after · or •
                                .replace(/\\s+/g, ' ')
                                .trim();
                        }
                        // Fallback: legacy class-based name selectors
                        if (!authorName) {
                            var legacyNameSels = [
                                '.feed-shared-actor__name', '.update-components-actor__name',
                                '[data-anonymize="person-name"]',
                                '.entity-result__title-text a span[aria-hidden="true"]',
                            ];
                            for (var ns = 0; ns < legacyNameSels.length; ns++) {
                                var nn = el.querySelector(legacyNameSels[ns]);
                                if (nn && nn.textContent.trim()) {
                                    authorName = nn.textContent.trim(); break;
                                }
                            }
                        }

                        // ── Author headline ───────────────────────────────────
                        // LinkedIn 2026: textContent has NO newlines (it's one long string).
                        // Headline appears after the degree indicator and before the time stamp.
                        // Pattern in textContent: "... NameText · 2ndHeadlineText1d • Follow..."
                        var headline = '';
                        var fullContainerText = el.textContent || '';
                        if (authorName) {
                            var namePos = fullContainerText.indexOf(authorName);
                            if (namePos >= 0) {
                                var afterName = fullContainerText.slice(namePos + authorName.length);
                                // Strip leading degree indicator (· 2nd, · 3rd+, • 2nd, etc.)
                                afterName = afterName.replace(/^[\s·•]+\d+(st|nd|rd|th)\+?[\s·•]*/i, '');
                                // Headline ends at the time pattern (e.g. 1d, 2d, 1w, 3mo, 1yr)
                                // followed optionally by • Edited, then • Follow or just Follow
                                var timeRx = /\s*\d+[dwhmyo][a-z]*[\s·•]*(Edited[\s·•]*)?(Follow|Repost)/;
                                var tIdx = afterName.search(timeRx);
                                if (tIdx > 0) {
                                    headline = afterName.slice(0, tIdx).trim();
                                } else {
                                    // No time marker — take up to 120 chars as best-effort headline
                                    headline = afterName.slice(0, 120).trim();
                                }
                            }
                        }
                        // Fallback: legacy headline selectors
                        if (!headline) {
                            var legacyHlSels = [
                                '.feed-shared-actor__sub-description',
                                '.update-components-actor__description',
                                '.artdeco-entity-lockup__subtitle',
                            ];
                            for (var hs = 0; hs < legacyHlSels.length; hs++) {
                                var hh = el.querySelector(legacyHlSels[hs]);
                                if (hh && hh.textContent.trim()) { headline = hh.textContent.trim(); break; }
                            }
                        }

                        // ── Post content ──────────────────────────────────────
                        // LinkedIn 2026: span[dir] is absent; post body is in the container
                        // textContent after the "Follow" button text (end of actor header).
                        // Strategy: extract from full container text, strip the header section.
                        var content = '';

                        // Strategy 1: child div NOT containing the /in/ link (post content div)
                        var elChildren = Array.from(el.children);
                        var maxChildLen = 0;
                        for (var dc = 0; dc < elChildren.length; dc++) {
                            var child = elChildren[dc];
                            if (child.querySelector('a[href*="/in/"]')) continue; // skip actor section
                            var childTxt = (child.textContent || '').trim();
                            if (childTxt.length > maxChildLen && childTxt.length > 20) {
                                maxChildLen = childTxt.length;
                                content = childTxt;
                            }
                        }

                        // Strategy 2: container textContent after "Follow" keyword (header ends there)
                        if (!content || content.length < 20) {
                            var fullCt = el.textContent || '';
                            var followIdx = fullCt.indexOf('Follow');
                            if (followIdx >= 0 && followIdx + 8 < fullCt.length) {
                                content = fullCt.slice(followIdx + 6).trim();
                            }
                        }

                        // Strategy 3: legacy class-based selectors (pre-2026 fallback)
                        if (!content || content.length < 20) {
                            var legacyContentSels = [
                                '.feed-shared-update-v2__description', '.feed-shared-text-view',
                                '.update-components-text', '.attributed-text-segment-list__content',
                                'span[dir="ltr"]', 'span[dir="rtl"]',
                            ];
                            for (var cs2 = 0; cs2 < legacyContentSels.length; cs2++) {
                                var cc = el.querySelector(legacyContentSels[cs2]);
                                if (cc && (cc.textContent || '').trim().length > 20) {
                                    content = cc.textContent.trim(); break;
                                }
                            }
                        }

                        // Strategy 4: full container text as last resort
                        if (!content || content.length < 20) {
                            content = (el.textContent || '').trim().slice(0, 800);
                        }

                        // ── Post URL ──────────────────────────────────────────
                        var postUrl = '';
                        var postLinkEl = el.querySelector('a[href*="/posts/"]') ||
                                         el.querySelector('a[href*="/feed/update/"]') ||
                                         el.querySelector('a.feed-shared-meta__link');
                        if (postLinkEl) {
                            postUrl = postLinkEl.href || postLinkEl.getAttribute('href') || '';
                        }

                        // ── Post date ─────────────────────────────────────────
                        var dateEl = el.querySelector('time') ||
                                     el.querySelector('[class*="time"]') ||
                                     el.querySelector('span.feed-shared-meta__item');
                        var postDate = dateEl ? dateEl.textContent.trim() : '';

                        // ── Public email from mailto: links ───────────────────
                        var mailtoLinks = Array.from(el.querySelectorAll('a[href^="mailto:"]'));
                        var rawEmail = '';
                        if (mailtoLinks.length > 0) {
                            rawEmail = (mailtoLinks[0].href || '').replace('mailto:', '').trim();
                        }

                        if (authorName || profileUrl) {
                            posts.push({
                                author_name:        authorName,
                                author_profile_url: profileUrl,
                                author_headline:    headline,
                                author_company:     '',
                                post_content:       content,
                                post_url:           postUrl,
                                post_date:          postDate,
                                raw_email:          rawEmail,
                            });
                        }
                    } catch (e) {
                        // Skip malformed post
                    }
                }
                return posts;
            }""")
        except Exception as exc:
            logger.warning("linkedin_page_js_extraction_failed", error=str(exc))
            return []
