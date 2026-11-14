import { csvCell } from "./csv";

describe("csvCell", () => {
  test("quotes plain values and doubles embedded quotes", () => {
    expect(csvCell("Acme")).toBe('"Acme"');
    expect(csvCell('say "hi"')).toBe('"say ""hi"""');
    expect(csvCell(42)).toBe('"42"');
  });

  test.each([
    ["=HYPERLINK(\"http://evil\",\"x\")", "'=HYPERLINK(\"\"http://evil\"\",\"\"x\"\")"],
    ["+SUM(A1:A2)", "'+SUM(A1:A2)"],
    ["-2+3+cmd|' /C calc'!A0", "'-2+3+cmd|' /C calc'!A0"],
    ["@SUM(1)", "'@SUM(1)"],
    ["\t=1+1", "'\t=1+1"],
    ["\r=1+1", "'\r=1+1"],
  ])("neutralises formula-like value %j", (input, escaped) => {
    expect(csvCell(input)).toBe('"' + escaped + '"');
  });

  test.each(["+91 9876543210", "+1 (555) 123-4567", "-12.5", "+919876543210"])(
    "leaves phone-like value %j unprefixed",
    (phone) => {
      expect(csvCell(phone)).toBe('"' + phone + '"');
    },
  );

  test("does not touch formula characters that aren't leading", () => {
    expect(csvCell("a=b")).toBe('"a=b"');
    expect(csvCell("—")).toBe('"—"');
  });
});
