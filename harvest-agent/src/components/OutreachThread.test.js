import { sanitizeThreadHtml } from "./OutreachThread";

describe("sanitizeThreadHtml", () => {
  test("strips scripts, event handlers and javascript: URLs", () => {
    const out = sanitizeThreadHtml(
      '<p onclick="alert(1)">hi</p><script>alert(2)</script><img src=x onerror="alert(3)"><a href="javascript:alert(4)">l</a>',
      true,
    );
    expect(out).not.toMatch(/script|onclick|onerror|javascript:/i);
    expect(out).toContain("hi");
  });

  test("forbids style/form/input/iframe tags", () => {
    const out = sanitizeThreadHtml(
      '<style>body{display:none}</style><form action="https://evil"><input name="p"></form><iframe src="https://evil"></iframe><b>ok</b>',
      false,
    );
    expect(out).not.toMatch(/<style|<form|<input|<iframe/i);
    expect(out).toContain("<b>ok</b>");
  });

  test("drops style attributes on inbound replies only", () => {
    const html = '<div style="position:fixed;inset:0">x</div>';
    expect(sanitizeThreadHtml(html, true)).not.toMatch(/style=/);
    expect(sanitizeThreadHtml(html, false)).toMatch(/style=/);
  });

  test("swaps the cid: signature logo for the public/ copy", () => {
    const out = sanitizeThreadHtml('<img src="cid:ss-logo" alt="logo">', false);
    expect(out).toContain('src="/sight_spectrum_logo.jpg"');
  });
});
