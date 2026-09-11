import { useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { rehypeCitations } from "./citations";
import { rehypeDisplayMath } from "./displaymath";
import { normaliseLatexDelimiters } from "./latexdelims";
import type { Citation } from "../../api/sse";

/** One prose style for every place learner-facing content is rendered — chat, notes, and item
 * stems — so a derivation does not look like a different product depending on which page it
 * arrived on. Wide content (code, tables) scrolls inside its own box rather than widening the
 * column: a long line in a code sample must not push the transcript sideways. */
const PROSE =
  "flex flex-col gap-3 [&_h1]:text-h2 [&_h2]:text-h3 [&_h3]:text-body [&_h3]:font-semibold " +
  "[&_p]:text-body [&_ul]:list-disc [&_ul]:pl-6 [&_ol]:list-decimal [&_ol]:pl-6 " +
  "[&_li]:text-body [&_strong]:font-semibold " +
  "[&_code]:bg-base-200 [&_code]:rounded-field [&_code]:px-1 [&_code]:text-[0.9em] " +
  "[&_pre]:bg-base-200 [&_pre]:rounded-box [&_pre]:p-3 [&_pre]:overflow-x-auto " +
  "[&_pre_code]:bg-transparent [&_pre_code]:p-0 " +
  "[&_blockquote]:border-primary/30 [&_blockquote]:text-base-content/70 " +
  "[&_blockquote]:border-l-2 [&_blockquote]:pl-4 " +
  "[&_table]:w-full [&_table]:text-body [&_th]:border-base-300 [&_th]:border-b [&_th]:text-left " +
  "[&_th]:p-2 [&_td]:border-base-300 [&_td]:border-b [&_td]:p-2 " +
  "[&_.katex-display]:overflow-x-auto [&_.katex-display]:overflow-y-hidden [&_.katex-display]:py-1";

/**
 * The renderer itself. Reached through RichText, which loads this lazily — KaTeX and the
 * unified pipeline are ~300kB, and before this split every visitor paid for them on the
 * landing page.
 *
 * Markdown, LaTeX, code, and clickable citations.
 *
 * Raw HTML is deliberately *not* enabled (no `rehype-raw`). Everything rendered here is either
 * model output or another learner's uploaded material, so the safe reading is that none of it
 * is trusted markup — react-markdown escapes HTML by default, and that default is the security
 * property, not an oversight to fix later.
 */
export default function RichTextContent({
  content,
  citations,
  onCitationClick,
  className,
}: {
  content: string;
  citations?: Citation[];
  onCitationClick?: (citation: Citation) => void;
  className?: string;
}) {
  const byMarker = useMemo(() => new Map((citations ?? []).map((c) => [c.marker, c])), [citations]);

  const rehypePlugins = useMemo(() => {
    // Skip the walk entirely when no caller can act on a citation — the `cite` component
    // below is what decides whether a marker becomes a control, this only avoids the work.
    // It runs before KaTeX so it can still tell which nodes are math and leave them alone;
    // afterwards that region is generated markup carrying no such marking.
    const plugins: NonNullable<React.ComponentProps<typeof ReactMarkdown>["rehypePlugins"]> = [
      rehypeDisplayMath,
    ];
    if (onCitationClick) plugins.push(rehypeCitations);
    // A stream delivers half an expression many times per turn, and a half-written `$$` is not
    // an error worth blanking the message for — render what parses and leave the rest as text.
    plugins.push([rehypeKatex, { throwOnError: false, output: "htmlAndMathml", strict: false }]);
    return plugins;
  }, [onCitationClick]);

  const components = useMemo<Components>(
    () => ({
      cite: ({ node, ...props }) => {
        void node;
        const marker = Number((props as Record<string, unknown>)["data-marker"]);
        const citation = byMarker.get(marker);
        // A number we hold no citation for is a hallucinated marker, or one whose citations
        // have not arrived yet mid-stream. It goes back exactly as the model wrote it: never a
        // control that looks live and does nothing.
        if (!citation || !onCitationClick) return <>[{marker}]</>;
        return (
          <button
            type="button"
            onClick={() => onCitationClick(citation)}
            aria-label={`Show source for citation ${marker}`}
            className="text-primary hover:bg-primary/15 focus-visible:ring-primary/50 mx-0.5 rounded px-1 align-super text-xs font-medium not-italic focus-visible:ring-2"
          >
            {marker}
          </button>
        );
      },
      // Model output and uploaded material can both carry links we did not write.
      a: ({ node, ...props }) => {
        void node;
        return (
          <a
            {...props}
            target="_blank"
            rel="noopener noreferrer nofollow"
            className="text-primary underline underline-offset-2"
          />
        );
      },
      table: ({ node, ...props }) => {
        void node;
        return (
          <div className="overflow-x-auto">
            <table {...props} />
          </div>
        );
      },
    }),
    [byMarker, onCitationClick],
  );

  return (
    <div className={className ? `${PROSE} ${className}` : PROSE}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={rehypePlugins}
        components={components}
      >
        {normaliseLatexDelimiters(content)}
      </ReactMarkdown>
    </div>
  );
}
