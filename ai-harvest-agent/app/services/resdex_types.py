"""Shared contract between the Resdex browser runner
(app/agents/resdex_candidate_agent.run_resdex_batch) and the match service
(app/services/resdex_match_service.prepare_resdex_candidates).

Candidate field names match ResdexCandidateORM columns (app/models/resdex.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

# Normalized candidate keys (subset of ResdexCandidateORM columns) a card may carry.
CANDIDATE_FIELDS = (
    "candidate_name", "headline", "current_designation", "current_company",
    "previous_roles", "experience_text", "experience_years", "location",
    "preferred_locations", "skills", "education", "notice_period", "ctc",
    "last_active", "profile_url",
)

# Resdex advSearch `phoneStatus` for a Naukri-verified phone. Matched live
# (2026-10-14) against the rendered card: phoneStatus 3 ⇔ "Verified phone & email"
# (title "Verified by naukri.com"), 0 ⇔ "Verified Email" only.
NAUKRI_PHONE_VERIFIED = 3


def naukri_verified(raw: Any) -> bool:
    """True only when Naukri itself marks the candidate's phone AND email verified:
    the stored card `raw` has emailVerified is True and phoneStatus == 3 (an int,
    never a bool/str), or — text cards — Naukri's rendered verification status
    mentions both "verified phone" and "email". Anything else (email-only,
    missing flags, rows stored before the flags were captured) is False.

    NOTE: not used for display — the email/job-page "Naukri Verified" badge is
    shown on every card (RESDEX_SHOW_NAUKRI_VERIFIED). Kept as the truthful
    per-candidate rule should the badge be gated again."""
    if not isinstance(raw, dict):
        return False
    status = raw.get("phoneStatus")
    if (raw.get("emailVerified") is True and type(status) is int
            and status == NAUKRI_PHONE_VERIFIED):
        return True
    text = str(raw.get("verificationStatus") or "").lower()
    return "verified phone" in text and "email" in text


# ── Resdex links ─────────────────────────────────────────────────────────────
# Evidence (live capture under data/debug/resdex/, 2026-10-14):
#  • A result card's own link is
#      /v3/preview?tabKey=profile&sid=<sid>&paramString=…&tupleIndex=<i>
#        &storageKey=<sid>-1&uresid=<dynamicEncryptedJsKey>&uniqId=<dynamicEncryptedUniqueId>…
#    Every token in it belongs to ONE search in ONE recruiter browser session:
#    the dynamicEncrypted* keys carry a segment shared by every tuple of that
#    search (absent from the static encryptedJsKey), and storageKey points at the
#    search context the results page kept in that browser's storage. Opened from
#    any other browser / later, Naukri rejects it ("link is not valid"), so it is
#    never shown as a profile link.
#  • Resdex's own "recent searches" list (results home page) reopens a past search
#    with  /v3/search?sid=<searchId>&source=advPage  — sids a week old are still
#    listed. That is the only reopenable link the captures prove; it needs the
#    same Naukri recruiter (sub-user) login and shows the result list, not a
#    single profile. No stable per-candidate profile URL appears in any capture.
RESDEX_ORIGIN = "https://resdex.naukri.com"
_SID_RE = re.compile(r"^\d{4,20}$")


def resdex_sid(value: Any) -> str:
    """A Resdex search id (all digits) as a string, or "" when `value` isn't one."""
    if isinstance(value, bool) or value is None:
        return ""
    s = str(value).strip()
    return s if _SID_RE.match(s) else ""


def resdex_results_url(sid: Any) -> str:
    """The reopenable Resdex result list of search `sid` (format Resdex itself uses
    for its recent-searches links), or "" when `sid` isn't a real search id."""
    s = resdex_sid(sid)
    return f"{RESDEX_ORIGIN}/v3/search?sid={s}&source=advPage" if s else ""


def is_session_bound_profile_url(url: Any) -> bool:
    """True for a Resdex result-card preview link (/v3/preview?…) — valid only in
    the browser session that ran the search, so never usable from the app."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    return "/v3/preview" in (parsed.path or "").lower()


def preview_link_context(url: Any) -> tuple[str, int | None]:
    """(sid, tupleIndex) read from a Resdex /v3/preview link — the search it came
    from and the card's 0-based place in that result list; ("", None) otherwise."""
    if not is_session_bound_profile_url(url):
        return "", None
    try:
        qs = parse_qs(urlparse(str(url).strip()).query)
    except ValueError:
        return "", None
    sid = resdex_sid((qs.get("sid") or qs.get("parentSid") or [""])[0])
    idx_s = (qs.get("tupleIndex") or [""])[0]
    idx = int(idx_s) if idx_s.isdigit() else None
    return sid, idx


def clean_profile_url(url: Any) -> str:
    """A candidate profile link safe to show: "" for session-bound /v3/preview links
    and anything that isn't an absolute http(s) URL."""
    s = str(url or "").strip()
    if not s or is_session_bound_profile_url(s):
        return ""
    try:
        scheme = urlparse(s).scheme.lower()
    except ValueError:
        return ""
    return s if scheme in ("http", "https") else ""


def resdex_link_context(profile_url: Any, raw: Any) -> tuple[str, int | None]:
    """(sid, 1-based position in the Resdex result list) for a stored candidate:
    from the card `raw` (sid / tupleIndex captured at search time) or, for rows
    stored before that, from the legacy /v3/preview link's own sid / tupleIndex."""
    sid, idx = "", None
    if isinstance(raw, dict):
        sid = resdex_sid(raw.get("sid"))
        t = raw.get("tupleIndex")
        idx = t if isinstance(t, int) and not isinstance(t, bool) and t >= 0 else None
    if not sid:
        sid, legacy_idx = preview_link_context(profile_url)
        if idx is None:
            idx = legacy_idx
    return sid, (idx + 1 if sid and idx is not None else None)


@dataclass
class ResdexTier:
    """One relaxation-ladder step: the Boolean query and the Resdex filters."""
    tier: int                        # 1-based
    query: str
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResdexJobQuery:
    job_url: str                     # normalized
    tiers: list[ResdexTier]          # tried in order until enough results


@dataclass
class ResdexCard:
    """One search-result card.

    method="json": `fields` is already normalized (CANDIDATE_FIELDS keys) from the
    intercepted Resdex results JSON. method="text": only `text` (the card's
    innerText) is known — the match service extracts fields with the LLM,
    grounded in that text. `profile_url` is a stable, absolute profile link when
    one is known — never a session-bound /v3/preview link (see above); the
    search id / result position live in `raw` ("sid", "tupleIndex")."""
    method: str                      # "json" | "text"
    fields: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    profile_url: str = ""
    raw: dict[str, Any] | None = None


@dataclass
class ResdexBatchResult:
    status: str                      # a ResdexSearchStatus value
    tier: int = 0                    # tier whose results are in `cards` (0 = none searched)
    searches_used: int = 0
    query: str = ""
    filters: dict[str, Any] = field(default_factory=dict)
    result_count: int = 0            # total results Resdex reported for that tier (if known)
    cards: list[ResdexCard] = field(default_factory=list)
    error: str | None = None
