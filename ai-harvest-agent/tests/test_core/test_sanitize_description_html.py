"""Tests for text_formatting.sanitize_description_html — the allow-list HTML
sanitizer used to store LinkedIn job-description rich HTML safely."""
from __future__ import annotations

from app.core.text_formatting import sanitize_description_html as s


def test_keeps_structural_tags() -> None:
    out = s("<h3>About</h3><p>Build <strong>things</strong>.</p><ul><li>A</li><li>B</li></ul>")
    assert "<h3>About</h3>" in out
    assert "<strong>things</strong>" in out
    assert "<ul>" in out and "<li>A</li>" in out


def test_strips_scripts_and_styles() -> None:
    out = s("<p>ok</p><script>alert(1)</script><style>.x{}</style>")
    assert "<p>ok</p>" in out
    assert "script" not in out.lower()
    assert "alert" not in out
    assert "<style" not in out.lower()


def test_drops_disallowed_tags_but_keeps_text() -> None:
    out = s("<div class='x'><section>hello <span>world</span></section></div>")
    # div/section unwrapped (dropped), text + allowed span preserved
    assert "hello" in out and "world" in out
    assert "<div" not in out and "<section" not in out


def test_strips_class_and_style_attrs() -> None:
    out = s('<p class="a b" style="color:red" onclick="evil()">x</p>')
    assert out == "<p>x</p>"


def test_neutralizes_javascript_href_and_hardens_links() -> None:
    out = s('<a href="javascript:evil()">x</a><a href="https://y.com">y</a>')
    assert "javascript:" not in out.lower()
    assert 'href="https://y.com"' in out
    assert 'rel="noopener noreferrer"' in out
    assert 'target="_blank"' in out


def test_empty_and_none() -> None:
    assert s("") == ""
    assert s("   ") == ""
    assert s(None) == ""
