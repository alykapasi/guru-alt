import type { Element, Root, RootContent } from "hast";

function hasClass(node: Element, name: string): boolean {
  const classes = node.properties?.className;
  return Array.isArray(classes) && classes.includes(name);
}

/** The one `<code class="math-inline">` a paragraph consists of, if that is all it consists of. */
function soleInlineMath(node: RootContent): Element | null {
  if (node.type !== "element" || node.tagName !== "p") return null;
  const substantive = node.children.filter((c) => !(c.type === "text" && c.value.trim() === ""));
  if (substantive.length !== 1) return null;
  const only = substantive[0];
  if (only.type !== "element" || only.tagName !== "code") return null;
  return hasClass(only, "math-inline") ? only : null;
}

/**
 * Promotes `$$E = mc^2$$` written on a line of its own to display math.
 *
 * remark-math treats `$$` as display only when the delimiters sit on their own lines, so
 * `$$\det(A - \lambda I) = 0$$` — how models and most authors write a standalone equation, and
 * how MathJax and GitHub both read it — arrived as cramped inline math inside a paragraph. We
 * promote only a paragraph containing *nothing else*, so `$$x$$` used mid-sentence still
 * renders inline, exactly as written.
 *
 * This works on the rehype tree rather than the remark one on purpose: remark-math attaches the
 * hast mapping when it parses, so a `math` node synthesised afterwards has none and degrades to
 * the raw TeX as text — which is both wrong on screen and, because the TeX is then ordinary
 * prose, enough to make a subscript like `x_{[1]}` look like a citation marker.
 */
export function rehypeDisplayMath() {
  return (tree: Root) => {
    for (let i = 0; i < tree.children.length; i++) {
      const code = soleInlineMath(tree.children[i]);
      if (!code) continue;
      tree.children[i] = {
        type: "element",
        tagName: "pre",
        properties: {},
        children: [{ ...code, properties: { className: ["language-math", "math-display"] } }],
      };
    }
  };
}
