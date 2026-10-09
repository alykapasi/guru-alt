import { describe, expect, it } from "vitest";
import { normaliseLatexDelimiters } from "./latexdelims";

/** What the rewrite may touch is decided by the Markdown parser, not a pattern: four spaces of
 * indentation are code after a blank line and ordinary continuation text inside a list item. */
describe("normaliseLatexDelimiters", () => {
  it("leaves an indented code block after a blank line alone", () => {
    const source = "Some prose.\n\n    \\(x\\) stays literal\n";
    expect(normaliseLatexDelimiters(source)).toBe(source);
  });

  it("leaves a tab-indented code block alone", () => {
    const source = "Prose.\n\n\t\\[y\\]\n";
    expect(normaliseLatexDelimiters(source)).toBe(source);
  });

  it("still rewrites a list item's indented continuation", () => {
    const source = "1. First point\n\n    where \\(x > 0\\) holds\n";
    expect(normaliseLatexDelimiters(source)).toBe("1. First point\n\n    where $x > 0$ holds\n");
  });

  it("leaves fenced and inline code alone", () => {
    const source = "```tex\n\\(a\\)\n```\nand `\\(b\\)` but \\(c\\)";
    expect(normaliseLatexDelimiters(source)).toBe("```tex\n\\(a\\)\n```\nand `\\(b\\)` but $c$");
  });

  it("rewrites display math across lines outside code", () => {
    expect(normaliseLatexDelimiters("\\[\na + b\n\\]")).toBe("$$a + b$$");
  });

  it("treats an unclosed fence mid-stream as code to the end", () => {
    const source = "```\n\\(x\\)";
    expect(normaliseLatexDelimiters(source)).toBe(source);
  });

  it("returns text with no delimiters untouched", () => {
    expect(normaliseLatexDelimiters("    plain")).toBe("    plain");
  });
});
