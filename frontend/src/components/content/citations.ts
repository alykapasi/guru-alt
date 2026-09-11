import type { Element, ElementContent, Root, RootContent } from "hast";

/** The literal [N] marker the backend writes into a message, mapped to the chunk it cites —
 * see app/services/turn_common.py's format_grounding/extract_citations. */
const CITATION_MARKER = /\[(\d+)\]/g;

/** Subtrees where a [N] is never a citation, and rewriting one would corrupt exactly the
 * content this renderer exists to display faithfully: code, where `arr[0]` is an index, and
 * TeX, where `x_{[1]}` is notation. One test covers both because remark-math represents an
 * equation *as* a `code` element (`<code class="language-math math-inline">`) — so the tag
 * check is the whole rule, and a class check on top of it was unreachable. */
function isOpaque(node: Element): boolean {
  return node.tagName === "code" || node.tagName === "pre";
}

/** Splits one text node on its markers. Returns null when it has none, so an untouched tree
 * keeps its original node objects. */
function split(value: string): ElementContent[] | null {
  CITATION_MARKER.lastIndex = 0;
  const parts: ElementContent[] = [];
  let cut = 0;
  let match: RegExpExecArray | null;
  while ((match = CITATION_MARKER.exec(value)) !== null) {
    if (match.index > cut) parts.push({ type: "text", value: value.slice(cut, match.index) });
    parts.push({
      type: "element",
      tagName: "cite",
      properties: { dataMarker: match[1] },
      children: [{ type: "text", value: match[1] }],
    });
    cut = match.index + match[0].length;
  }
  if (parts.length === 0) return null;
  if (cut < value.length) parts.push({ type: "text", value: value.slice(cut) });
  return parts;
}

/**
 * Rewrites every [N] marker into a `<cite data-marker="N">` element. Whether a given marker is
 * one we actually hold a citation for is RichText's call, not this plugin's — deciding it in
 * both places meant neither decision could be tested, because removing either one left the
 * rendered output identical.
 *
 * This runs over the parsed tree rather than over the raw string, which is what makes it safe
 * against Markdown's own bracket syntax: by the time we see the tree, `[1](https://example.com)`
 * is already an `<a>` and `[1]: https://example.com` is already a link definition, so neither
 * can be mistaken for a citation. Splitting the string first — the approach this replaced —
 * also broke every block structure a marker happened to sit inside.
 */
export function rehypeCitations() {
  return (tree: Root) => {
    const walk = (node: Root | Element) => {
      const children: RootContent[] = [];
      let changed = false;
      for (const child of node.children) {
        if (child.type === "text") {
          const parts = split(child.value);
          if (parts) {
            children.push(...parts);
            changed = true;
            continue;
          }
        } else if (child.type === "element" && !isOpaque(child)) {
          walk(child);
        }
        children.push(child);
      }
      if (changed) node.children = children as Element["children"];
    };
    walk(tree);
  };
}
