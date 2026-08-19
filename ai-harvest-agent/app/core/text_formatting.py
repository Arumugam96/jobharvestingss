"""Post-processing formatter for job-description text.

The extraction LLM (Claude or the local Ollama model — whichever
EXTRACTION_LLM_MODEL selects, see LLMService) is only ever asked for plain
text, never HTML or markdown. Formatting happens here instead, deterministically,
on whatever text comes back from *any* source (direct Playwright scrape or the
LLM fallback) — so the result is consistent across providers and safe to drop
straight into an Excel cell or a JSON API response without needing HTML
sanitization.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

_BULLET_PREFIX = re.compile(
    r"^(?:[•‣▪●○◦*·]|-|\d{1,2}[.)]|\([a-zA-Z0-9]\))\s+"
)
# LinkedIn/board scrapes sometimes glue bullet items into one run of text
# with no line breaks (e.g. "• Item one• Item two") — force each glyph onto
# its own line before splitting into lines.
_INLINE_BULLET_GLYPH = re.compile(r"\s*([•‣▪●○◦])\s*")
# Short "Responsibilities:" / "Requirements:" style section headers — get a
# blank line before them for visual separation.
_SECTION_HEADER = re.compile(r"^[A-Za-z][A-Za-z0-9 /&()'\-]{2,60}:$")


def format_job_description(text: str) -> str:
    """Normalize raw job-description text into clean paragraphs and bullets.

    - Collapses runs of blank lines to a single blank line.
    - Normalizes any bullet-ish line prefix (-, *, •, 1., (a), etc.) to "• ".
    - Splits bullet glyphs glued into one run of text onto their own lines.
    - Adds a blank line before short "Section:" headers for readability.

    Returns "" for empty/whitespace-only input. Provider-agnostic — call this
    on the final description text regardless of whether it came from a raw
    DOM scrape or an LLM extraction call.
    """
    if not text or not text.strip():
        return ""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _INLINE_BULLET_GLYPH.sub(r"\n\1 ", normalized)

    out_lines: list[str] = []
    blank_run = 0
    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            blank_run += 1
            if blank_run == 1 and out_lines:
                out_lines.append("")
            continue

        if blank_run == 0 and out_lines and _SECTION_HEADER.match(line):
            out_lines.append("")
        blank_run = 0

        match = _BULLET_PREFIX.match(line)
        if match:
            out_lines.append("• " + line[match.end():].strip())
        else:
            out_lines.append(line)

    while out_lines and out_lines[0] == "":
        out_lines.pop(0)
    while out_lines and out_lines[-1] == "":
        out_lines.pop()

    return "\n".join(out_lines)


# ── HTML sanitization (job-description rich rendering) ─────────────────────────

# Structural/formatting tags kept for display. Everything else is unwrapped
# (text preserved, tag dropped) or, for dangerous containers, decomposed.
_ALLOWED_HTML_TAGS = {
    "p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u",
    "h1", "h2", "h3", "h4", "span", "a",
}
_ALLOWED_HTML_ATTRS: dict[str, set[str]] = {"a": {"href", "target", "rel"}}
_DROP_HTML_CONTAINERS = [
    "script", "style", "noscript", "iframe", "svg", "img",
    "button", "input", "form", "link", "meta",
]


def sanitize_description_html(html: str) -> str:
    """Sanitize scraped job-description HTML down to a safe display subset.

    Keeps only structural/formatting tags (paragraphs, lists, emphasis,
    headings, links); strips scripts/styles/event handlers/inline styles and
    any tag or attribute not on the allow-list. Links are forced to
    rel="noopener noreferrer" target="_blank" and javascript: hrefs removed.
    Returns "" for empty/whitespace input or on any parse error. Uses bs4 +
    lxml, already project dependencies — no new package. The frontend also runs
    DOMPurify as defense-in-depth.
    """
    if not html or not html.strip():
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return ""

    for tag in soup(_DROP_HTML_CONTAINERS):
        tag.decompose()

    for tag in soup.find_all(True):
        if tag.name not in _ALLOWED_HTML_TAGS:
            tag.unwrap()  # keep the text, drop the tag
            continue
        allowed = _ALLOWED_HTML_ATTRS.get(tag.name, set())
        for attr in list(tag.attrs):
            if attr not in allowed:
                del tag[attr]
        if tag.name == "a":
            href = (tag.get("href") or "").strip()
            if href.lower().startswith("javascript:"):
                del tag["href"]
            else:
                tag["target"] = "_blank"
                tag["rel"]    = "noopener noreferrer"

    container = soup.body or soup
    return container.decode_contents().strip()
