import { lazy, Suspense } from "react";
import type { Citation } from "../../api/sse";

export interface RichTextProps {
  content: string;
  citations?: Citation[];
  onCitationClick?: (citation: Citation) => void;
  className?: string;
}

// Markdown, math and the unified pipeline are around 300kB — a third of the bundle, and of no
// use at all until the learner is looking at content. Kept out of the initial download.
const RichTextContent = lazy(() => import("./RichTextContent"));

/**
 * The single entry point for rendering learner-facing content: Markdown, LaTeX, code, and
 * clickable citations (S53).
 *
 * The fallback is the text itself, not a spinner. A reply is worth reading before it is worth
 * typesetting, and during a stream this component renders many times a second — a placeholder
 * would mean the message visibly disappears on first paint.
 */
export function RichText(props: RichTextProps) {
  return (
    <Suspense fallback={<p className="text-body whitespace-pre-wrap">{props.content}</p>}>
      <RichTextContent {...props} />
    </Suspense>
  );
}
