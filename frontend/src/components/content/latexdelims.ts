import { fromMarkdown } from "mdast-util-from-markdown";
import { gfm } from "micromark-extension-gfm";
import { gfmFromMarkdown } from "mdast-util-gfm";
import { math } from "micromark-extension-math";
import { mathFromMarkdown } from "mdast-util-math";

const INLINE = /\\\((.+?)\\\)/g;
const DISPLAY = /\\\[([\s\S]+?)\\\]/g;

/** Nodes whose text the learner is meant to read exactly as written. */
const VERBATIM = new Set(["code", "inlineCode", "math", "inlineMath"]);

interface Node {
  type: string;
  position?: { start: { offset?: number }; end: { offset?: number } };
  children?: Node[];
}

function rewrite(prose: string): string {
  return prose
    .replace(DISPLAY, (_, body: string) => `$$${body.trim()}$$`)
    .replace(INLINE, (_, body: string) => `$${body.trim()}$`);
}

/** Source ranges of every code and math node, in document order. */
function verbatimRanges(source: string): Array<[number, number]> {
  const tree = fromMarkdown(source, {
    extensions: [gfm(), math()],
    mdastExtensions: [gfmFromMarkdown(), mathFromMarkdown()],
  }) as Node;
  const ranges: Array<[number, number]> = [];
  const walk = (node: Node) => {
    const start = node.position?.start.offset;
    const end = node.position?.end.offset;
    if (VERBATIM.has(node.type) && start !== undefined && end !== undefined) {
      ranges.push([start, end]);
      return;
    }
    node.children?.forEach(walk);
  };
  walk(tree);
  return ranges;
}

/**
 * Rewrites LaTeX's `\(x\)` and `\[x\]` delimiters to the `$x$` and `$$x$$` remark-math parses.
 *
 * Models emit this pair constantly — it is what LaTeX itself prescribes — and without this an
 * otherwise correct derivation arrives with `\(2\times2\)` sitting in the prose as literal
 * backslashes.
 *
 * It has to happen here, on the source, rather than as a plugin over the parsed tree: `\(` is a
 * CommonMark escape for a literal paren, so by the time any plugin runs the backslashes are
 * gone and `(x)` is indistinguishable from ordinary parentheses. Converting to `$` rather than
 * to some sentinel of our own matters for the same reason in reverse — remark-math parses the
 * body with its own micromark extension, so `a_1` inside stays a subscript instead of being
 * read as Markdown emphasis.
 *
 * Where code is, the parser decides (S53): four spaces of indentation are a code block after a
 * blank line and ordinary continuation text inside a list item, which no pattern can tell
 * apart. Text with no delimiter is returned without parsing.
 */
export function normaliseLatexDelimiters(source: string): string {
  if (!source.includes("\\(") && !source.includes("\\[")) return source;
  let out = "";
  let cut = 0;
  for (const [start, end] of verbatimRanges(source)) {
    out += rewrite(source.slice(cut, start)) + source.slice(start, end);
    cut = end;
  }
  return out + rewrite(source.slice(cut));
}
