"""Allow-list sanitizer for inbound email HTML (prospect replies).

Inbound reply HTML (Brevo inbound-parse RawHtmlBody/HtmlBody, or the IMAP/Graph html
part) is attacker-controlled: anyone can email the reply mailbox. The Mail-logs thread
renders ``body_html`` as HTML, so it is reduced here to a safe, email-appropriate subset
both when a reply is stored and again when it is served (covering rows stored before
this sanitizer existed). The frontend runs DOMPurify on top as defense-in-depth.

Modelled on app.core.text_formatting.sanitize_description_html (bs4 + lxml, both
existing dependencies), with an allow-list suited to email: basic formatting, links
(http/https/mailto only), tables, and images whose src is https: or cid: (the
signature logo is ``cid:ss-logo``, which the frontend swaps for the hosted image).
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, CData, Comment, Declaration, Doctype, ProcessingInstruction

# Tags whose CONTENT is dropped along with the tag (active content, embeds, metadata).
_DROP_WITH_CONTENT = [
    "script", "style", "iframe", "frame", "frameset", "form", "input", "button",
    "select", "textarea", "option", "object", "embed", "applet", "svg", "math",
    "link", "meta", "base", "noscript", "template", "title", "head", "audio",
    "video", "canvas", "param", "source", "track", "portal",
]

# Tags kept as-is (attributes filtered). Anything else is unwrapped (text kept).
_ALLOWED_TAGS = {
    "p", "br", "div", "span", "a", "b", "strong", "i", "em", "u", "s", "strike",
    "del", "ins", "sub", "sup", "small", "big", "font", "center", "blockquote",
    "pre", "code", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
    "dl", "dt", "dd", "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "caption", "colgroup", "col", "img", "abbr", "cite", "q", "address",
}

_GLOBAL_ATTRS = {"title", "dir", "lang", "align"}
_TAG_ATTRS: dict[str, set[str]] = {
    "a": {"href"},
    "img": {"src", "alt", "width", "height"},
    "font": {"color", "size", "face"},
    "table": {"border", "cellpadding", "cellspacing", "width", "valign"},
    "td": {"colspan", "rowspan", "valign", "width", "height"},
    "th": {"colspan", "rowspan", "valign", "width", "height"},
    "tr": {"valign"},
    "col": {"span", "width"},
    "colgroup": {"span", "width"},
    "ol": {"start", "type"},
}

_LINK_SCHEMES = ("http:", "https:", "mailto:")
_IMG_SCHEMES = ("https:", "cid:")
# Browsers ignore ASCII whitespace/control chars inside a URL scheme ("java\tscript:"),
# so strip them before deciding which scheme a URL really uses.
_URL_NOISE_RE = re.compile(r"[\x00-\x20\x7f]+")


def _url_allowed(value: str, schemes: tuple[str, ...]) -> bool:
    """True when ``value`` uses one of ``schemes`` (case-insensitive, noise-stripped)."""
    cleaned = _URL_NOISE_RE.sub("", value or "").lower()
    return cleaned.startswith(schemes)


def sanitize_email_html(html: str | None) -> str:
    """Reduce untrusted email HTML to a safe display subset.

    Removes script/style/iframe/frame/form/input/button/object/embed/svg/math/link/
    meta/base (and similar) together with their content, comments and doctypes;
    unwraps any other non-allow-listed tag (keeping its text); drops every attribute
    not on the per-tag allow-list — so all ``on*`` handlers and ``style`` go. Links
    keep only http/https/mailto hrefs and are forced to ``target="_blank"`` +
    ``rel="noopener noreferrer"``; images keep only an https: or cid: src (an image
    without one is removed). Returns "" for empty input or on any parse error (fail
    closed)."""
    if not html or not str(html).strip():
        return ""
    try:
        soup = BeautifulSoup(str(html), "lxml")
        for node in soup.find_all(
            string=lambda s: isinstance(s, (Comment, CData, Declaration, Doctype,
                                            ProcessingInstruction))
        ):
            node.extract()
        for tag in soup.find_all(_DROP_WITH_CONTENT):
            tag.decompose()

        for tag in soup.find_all(True):
            if tag.name not in _ALLOWED_TAGS:
                tag.unwrap()
                continue
            allowed = _GLOBAL_ATTRS | _TAG_ATTRS.get(tag.name, set())
            for attr in list(tag.attrs):
                if attr.lower() not in allowed:
                    del tag[attr]
            if tag.name == "a":
                href = str(tag.get("href") or "")
                if href and _url_allowed(href, _LINK_SCHEMES):
                    tag["href"] = href.strip()
                    tag["target"] = "_blank"
                    tag["rel"] = "noopener noreferrer"
                elif tag.has_attr("href"):
                    del tag["href"]
            elif tag.name == "img":
                src = str(tag.get("src") or "")
                if not _url_allowed(src, _IMG_SCHEMES):
                    tag.decompose()
                    continue
                tag["src"] = src.strip()

        container = soup.body or soup
        return container.decode_contents().strip()
    except Exception:
        return ""
