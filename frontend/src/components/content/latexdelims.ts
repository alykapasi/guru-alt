/** Fenced blocks and inline spans, in source order — the regions a rewrite must not touch. */
const CODE_REGION = /^ {0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:^ {0,3}\1[^\n]*$|$)|(`+)[\s\S]*?\2/gm;

const INLINE = /\\\((.+?)\\\)/g;
const DISPLAY = /\\\[([\s\S]+?)\\\]/g;

function rewrite(prose: string): string {
  return prose
    .replace(DISPLAY, (_, body: string) => `$$${body.trim()}$$`)
    .replace(INLINE, (_, body: string) => `$${body.trim()}$`);
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
 * Code is skipped by scanning it out first: `\(` inside a fence is something the learner is
 * meant to read. Indented (four-space) code blocks are not detected — see the tracker entry.
 */
export function normaliseLatexDelimiters(source: string): string {
  if (!source.includes("\\(") && !source.includes("\\[")) return source;
  let out = "";
  let cut = 0;
  CODE_REGION.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = CODE_REGION.exec(source)) !== null) {
    out += rewrite(source.slice(cut, match.index)) + match[0];
    cut = match.index + match[0].length;
  }
  return out + rewrite(source.slice(cut));
}
