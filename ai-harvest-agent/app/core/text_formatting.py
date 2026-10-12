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
from html import escape as _escape
from typing import Any

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

_BULLET_PREFIX = re.compile(r"^(?:[•‣▪●○◦*·]|-|\d{1,2}[.)]|\([a-zA-Z0-9]\))\s+")
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
# Deliberately broad: the LLM fallback is prompted to reproduce LinkedIn's
# on-page rendering of the description as faithfully as possible (any layout
# tags), so generic containers, all heading levels, tables, quotes and
# separators must survive sanitization. Safety comes from what is DROPPED
# (scripts/styles/iframes/etc. below) and from stripping every attribute
# except a[href] — not from a minimal tag set. DOMPurify re-sanitizes in the
# frontend as defense-in-depth.
_ALLOWED_HTML_TAGS = {
    "p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u",
    "h1", "h2", "h3", "h4", "h5", "h6", "span", "a",
    "div", "section", "article", "table", "thead", "tbody", "tfoot", "tr",
    "th", "td", "blockquote", "pre", "code", "hr", "sub", "sup", "small",
    "dl", "dt", "dd",
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


# ── Deterministic plain-text → HTML (job-description fallback) ─────────────────

def description_text_to_html(text: str) -> str:
    """Convert already-formatted plain-text job-description into safe HTML.

    Input is the output of format_job_description (or any similarly structured
    text): `• ` bullet lines, short "Header:" lines, and blank-line-separated
    paragraphs. Produces `<ul><li>…</li></ul>`, `<h3>…</h3>`, and `<p>…</p>`
    blocks. All text is HTML-escaped, so the result is safe to render. Returns
    "" for empty/whitespace input.

    Used as the guaranteed fallback for job_description_html when neither the
    DOM capture nor the LLM's own HTML is available — the text already came from
    the extraction LLM, so this is "the LLM's content in an aesthetic format".
    """
    if not text or not text.strip():
        return ""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    para: list[str] = []
    bullets: list[str] = []

    def _flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(_escape(l) for l in para) + "</p>")
            para.clear()

    def _flush_bullets() -> None:
        if bullets:
            out.append("<ul>" + "".join(f"<li>{_escape(b)}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            _flush_bullets()
            _flush_para()
            continue

        bullet = _BULLET_PREFIX.match(line)
        if bullet:
            _flush_para()
            bullets.append(line[bullet.end():].strip())
            continue

        # Non-bullet line ends any open list.
        _flush_bullets()
        if _SECTION_HEADER.match(line):
            _flush_para()
            out.append(f"<h3>{_escape(line)}</h3>")
        else:
            para.append(line)

    _flush_bullets()
    _flush_para()
    return "".join(out)


# ── Deterministic HTML → plain text (read-time report/outreach fallback) ───────

def html_description_to_text(html: str) -> str:
    """Convert stored job-description HTML back into clean formatted plain text.

    The reverse of description_text_to_html: <li> items become "• " bullets,
    <br> and block-level tags (p/div/list/heading) become line breaks, while
    inline tags (strong/em/a/span) are unwrapped without breaking the sentence
    they sit in. The result is run through format_job_description for consistent
    bullet/paragraph normalization.

    Used at READ time (reports, outreach) for LinkedIn jobs where only
    job_description_html was stored and the plain-text job_description is empty —
    so the tag-free JSON/Excel downloads and the outreach prompt still get the
    description without asking the LLM for a separate verbatim copy. Returns ""
    for empty/whitespace input or on any parse error.
    """
    if not html or not html.strip():
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return ""

    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert(0, "• ")
    # Trailing newline after each block element so get_text() (which uses no
    # separator, keeping inline runs intact) still breaks between blocks.
    # Every block-level tag counts — a missing one (h5, td, section, dd, …)
    # glues the next block's first word onto this one's last ("x@acme.comCTC").
    for block in soup.find_all(_TEXT_BLOCK_TAGS):
        block.append("\n")
    # Paragraphs (outside list items) and headings are separated from what
    # follows/precedes by a blank line, as they render.
    for para in soup.find_all("p"):
        if para.find_parent("li") is None:
            para.append("\n")
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        heading.insert(0, "\n")
    # Table cells sit side by side: a space keeps "CTC" and "Industry" apart.
    for cell in soup.find_all(["td", "th"]):
        cell.append(" ")

    return format_job_description(soup.get_text())


# Block-level tags that end a line in html_description_to_text.
_TEXT_BLOCK_TAGS = [
    "p", "div", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr",
    "table", "section", "article", "header", "footer", "aside", "nav", "main",
    "blockquote", "pre", "hr", "dl", "dt", "dd", "address", "figure",
    "figcaption", "center", "form", "fieldset",
]


# ── Structure repair for authored description HTML (Naukri) ─────────────────────

_STRUCTURE_BLOCK_TAGS = frozenset(_TEXT_BLOCK_TAGS) | {"thead", "tbody", "tfoot", "td", "th", "li"}
_EMPHASIS_TAGS = frozenset({"strong", "b", "u", "h1", "h2", "h3", "h4", "h5", "h6"})
_WRAPPER_TAGS = frozenset({"p", "div", "span", "font"})
_RAW_TEXT_PARENTS = frozenset({"pre", "code", "script", "style", "textarea"})
_LINE_BULLET_RE = re.compile(r"^\s*(?:[•‣▪●○◦*·]|-)\s+")
_GLYPHS = "•‣▪●○◦"
_GLYPH_SPLIT_RE = re.compile(rf"\s*([{_GLYPHS}])\s*")
_NEWLINE_SEP_RE = re.compile(r"(\s*\n\s*)")
_HEADING_MAX_CHARS = 80


def _seg_text(nodes: list) -> str:
    raw = "".join(n.get_text() if isinstance(n, Tag) else str(n) for n in nodes)
    return " ".join(raw.split())


def _split_on_br(nodes: list) -> list[list]:
    """Split a child-node list at <br> tags (blank segments kept, so a double
    <br> shows up as an empty segment = paragraph break)."""
    segs: list[list] = [[]]
    for n in nodes:
        if isinstance(n, Tag) and n.name == "br":
            segs.append([])
        else:
            segs[-1].append(n)
    return segs


def _is_heading_segment(nodes: list) -> bool:
    """A line that is a section heading: "Minimum Qualifications:" style, or a
    short line that is entirely bold/underlined ("<b>Roles and Responsibilities</b>")."""
    text = _seg_text(nodes)
    if not text or len(text) > _HEADING_MAX_CHARS:
        return False
    if _SECTION_HEADER.match(text):
        return True
    significant = [n for n in nodes if not (isinstance(n, NavigableString) and not n.strip())]
    return (
        len(significant) == 1 and isinstance(significant[0], Tag)
        and significant[0].name in _EMPHASIS_TAGS
        and len(text) <= 60 and not text.endswith((".", ",", ";"))
    )


def _is_bullet_segment(nodes: list) -> bool:
    return bool(_LINE_BULLET_RE.match(_seg_text(nodes)))


def _strip_bullet_glyph(nodes: list) -> list:
    """Segment with the typed bullet ("- ", "• ") removed from its first text."""
    out = list(nodes)
    for i, n in enumerate(out):
        if isinstance(n, NavigableString):
            if not n.strip():
                continue
            fresh = NavigableString(_LINE_BULLET_RE.sub("", str(n), count=1))
            n.replace_with(fresh)
            out[i] = fresh
            return out
        if isinstance(n, Tag):
            target = next((s for s in n.find_all(string=True) if s.strip()), None)
            if target is None:
                continue
            target.replace_with(NavigableString(_LINE_BULLET_RE.sub("", str(target), count=1)))
            return out
    return out


def _newlines_to_br(root: Tag, soup: BeautifulSoup) -> None:
    """Authored line breaks typed as raw "\\n" (and bullet glyphs glued into one
    run) become real <br>s, so they survive HTML's whitespace collapsing.
    Whitespace-only nodes between tags are left alone; a break at the very
    start/end of a text node becomes a plain space."""
    for node in list(root.find_all(string=True)):
        if isinstance(node, Comment) or not node.strip():
            continue
        if node.parent is not None and node.parent.name in _RAW_TEXT_PARENTS:
            continue
        text = str(node)
        if sum(text.count(g) for g in _GLYPHS) >= 2:
            text = _GLYPH_SPLIT_RE.sub(r"\n\1 ", text)
        if "\n" not in text:
            continue
        tokens = _NEWLINE_SEP_RE.split(text)       # odd indices = separators
        new: list = []
        for idx, tok in enumerate(tokens):
            if idx % 2 == 0:
                if tok:
                    new.append(NavigableString(tok))
                continue
            at_edge = not any(tokens[0:idx:2]) or not any(tokens[idx + 1::2])
            if at_edge:
                new.append(NavigableString(" "))
                continue
            new.append(soup.new_tag("br"))
            if tok.count("\n") >= 2:
                new.append(soup.new_tag("br"))
        if new:
            node.replace_with(*new)


def _unwrap_single_wrapper(li: Tag) -> None:
    """<li><p>a<br>b</p></li> → <li>a<br>b</li> (Word-paste wrappers)."""
    while True:
        significant = [c for c in li.contents
                       if not (isinstance(c, NavigableString) and not c.strip())]
        if (len(significant) == 1 and isinstance(significant[0], Tag)
                and significant[0].name in _WRAPPER_TAGS):
            significant[0].unwrap()
            continue
        return


def _split_list_items(lst: Tag, soup: BeautifulSoup) -> None:
    """One <li> holding several <br>-separated lines → one <li> per line.

    Recruiters often paste a whole section into a single bullet, so only the
    first line renders as a bullet and the next section's heading ("Minimum
    Qualifications:") runs on as the last line. Each line becomes its own item;
    a heading line closes the list, is emitted as <h3>, and any lines after it
    start a fresh list."""
    for li in lst.find_all("li", recursive=False):
        _unwrap_single_wrapper(li)
    if not any(isinstance(c, Tag) and c.name == "br"
               for li in lst.find_all("li", recursive=False) for c in li.contents):
        return

    blocks: list[tuple[str, Any]] = []
    for child in list(lst.contents):
        if isinstance(child, Tag) and child.name == "li":
            segs = [s for s in _split_on_br(list(child.contents)) if _seg_text(s)]
            if len(segs) <= 1:
                blocks.append(("whole", child))
                continue
            for seg in segs:
                blocks.append(("heading" if _is_heading_segment(seg) else "item", seg))
        elif isinstance(child, NavigableString) and not child.strip():
            continue
        else:
            blocks.append(("whole", child))

    new_elements: list[Tag] = []
    current: Tag | None = None
    for kind, payload in blocks:
        if kind == "heading":
            current = None
            h = soup.new_tag("h3")
            for n in payload:
                h.append(n)
            new_elements.append(h)
            continue
        if current is None:
            current = soup.new_tag(lst.name)
            new_elements.append(current)
        if kind == "whole":
            current.append(payload)
        else:
            li = soup.new_tag("li")
            for n in payload:
                li.append(n)
            current.append(li)
    if new_elements:
        lst.replace_with(*new_elements)


def _rebuild_line_run(nodes: list, soup: BeautifulSoup) -> list[Tag] | None:
    """<br>-separated lines → paragraphs / <h3> headings / <ul> for typed
    bullets. None when the run has no heading or typed-bullet line (left as is)."""
    segs = _split_on_br(nodes)
    kinds = []
    for seg in segs:
        if not _seg_text(seg):
            kinds.append("blank")
        elif _is_heading_segment(seg):
            kinds.append("heading")
        elif _is_bullet_segment(seg):
            kinds.append("bullet")
        else:
            kinds.append("line")
    if not any(k in ("heading", "bullet") for k in kinds):
        return None

    out: list[Tag] = []
    para: Tag | None = None
    ul: Tag | None = None
    for seg, kind in zip(segs, kinds):
        if kind == "blank":
            para = ul = None
            continue
        if kind == "heading":
            para = ul = None
            h = soup.new_tag("h3")
            for n in seg:
                h.append(n)
            out.append(h)
        elif kind == "bullet":
            para = None
            if ul is None:
                ul = soup.new_tag("ul")
                out.append(ul)
            seg = _strip_bullet_glyph(seg)
            li = soup.new_tag("li")
            for n in seg:
                li.append(n)
            ul.append(li)
        else:
            ul = None
            if para is None:
                para = soup.new_tag("p")
                out.append(para)
            elif para.contents:
                para.append(soup.new_tag("br"))
            for n in seg:
                para.append(n)
    return out


def _has_block_child(tag: Tag) -> bool:
    return any(isinstance(c, Tag) and c.name in _STRUCTURE_BLOCK_TAGS for c in tag.children)


def _split_paragraph_lines(root: Tag, soup: BeautifulSoup) -> None:
    """Split headings / typed bullets out of <br>-separated <p>/<div> lines and
    out of bare inline runs sitting directly in the root."""
    for container in reversed(root.find_all(["p", "div"])):
        if container.find("br", recursive=False) is None or _has_block_child(container):
            continue
        rebuilt = _rebuild_line_run(list(container.contents), soup)
        if rebuilt:
            container.replace_with(*rebuilt)

    run: list = []

    def _flush() -> None:
        if run and any(isinstance(n, Tag) and n.name == "br" for n in run):
            rebuilt = _rebuild_line_run(list(run), soup)
            if rebuilt:
                run[0].insert_before(*rebuilt)
                for n in run:
                    if n.parent is not None:
                        n.extract()
        run.clear()

    for child in list(root.contents):
        if isinstance(child, Tag) and child.name in _STRUCTURE_BLOCK_TAGS:
            _flush()
        else:
            run.append(child)
    _flush()


def restructure_description_html(html: str) -> str:
    """Repair the authoring defects that flatten job-board description HTML.

    Deterministic, content-preserving (only tags are added/moved — no text is
    changed, beyond dropping a typed "- "/"• " marker that becomes a real
    bullet). Used for Naukri, whose descriptions are recruiter-authored:

      * raw "\\n" line breaks inside text → <br> (HTML would collapse them);
      * one <li> holding several <br>-separated lines → one <li> per line, with
        a heading line ("Minimum Qualifications:") closing the list as <h3>;
      * in <br>-separated paragraphs, heading lines are split out as <h3> and
        typed "- "/"• " lines become a real <ul>.

    Returns unsanitized HTML (run sanitize_description_html afterwards), or the
    input unchanged when it can't be parsed.
    """
    if not html or not html.strip():
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return html
    root = soup.body or soup
    _newlines_to_br(root, soup)
    for lst in reversed(root.find_all(["ul", "ol"])):
        _split_list_items(lst, soup)
    _split_paragraph_lines(root, soup)
    return root.decode_contents().strip()


# ── Job-title normalization (LinkedIn "verified" a11y artifact) ─────────────────

# LinkedIn renders a "verified job" title anchor with two child spans — a visible
# one and a visually-hidden accessibility label that appends " with verification":
#   <a class="job-card-list__title--link">
#     <span aria-hidden="true">Windows Device Driver</span>
#     <span class="visually-hidden">Windows Device Driver with verification</span>
#   </a>
# Playwright's inner_text() returns BOTH spans (the hidden one is clipped, not
# display:none), so a scraped title can arrive as "<Title> <Title> with verification"
# (or just "<Title> with verification"). These strip that artifact deterministically.
_VERIFIED_SUFFIX_RE = re.compile(r"\s*with verification\s*$", re.IGNORECASE)
_TITLE_DUP_RE = re.compile(r"^(.+?)\s+\1$")


def normalize_job_title(title: str) -> str:
    """Strip LinkedIn's visually-hidden '<Title> with verification' accessibility
    text (and the immediate title duplication it causes) from a scraped job title.

        "Windows Device Driver Windows Device Driver with verification" -> "Windows Device Driver"
        "Windows Device Driver with verification"                       -> "Windows Device Driver"

    Only activates when the trailing " with verification" marker is present, so a
    legitimately worded title is returned unchanged (only stripped of surrounding
    whitespace). Idempotent — safe to call more than once."""
    t = (title or "").strip()
    if not _VERIFIED_SUFFIX_RE.search(t):
        return t
    # Collapse any internal whitespace (incl. a newline the scraper left between the
    # two spans) so the duplication check works, then drop the a11y suffix + one copy.
    t = " ".join(t.split())
    core = _VERIFIED_SUFFIX_RE.sub("", t).strip()
    dup = _TITLE_DUP_RE.match(core)
    return (dup.group(1) if dup else core).strip()
