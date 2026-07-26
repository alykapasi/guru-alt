import { Sprout } from "lucide-react";
import type { Citation } from "../../api/sse";

const CITATION_MARKER = /\[(\d+)\]/g;

/** Splits `content` on [N] markers, rendering matched ones as small clickable superscripts and
 * unmatched ones (a hallucinated number, or markers before `citations` has arrived — see
 * MessageList) as plain inert text — never a broken-looking link. */
function renderWithCitations(
  content: string,
  citations: Citation[],
  onCitationClick: (citation: Citation) => void,
) {
  const byMarker = new Map(citations.map((c) => [c.marker, c]));
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  CITATION_MARKER.lastIndex = 0;
  while ((match = CITATION_MARKER.exec(content)) !== null) {
    if (match.index > lastIndex) parts.push(content.slice(lastIndex, match.index));
    const citation = byMarker.get(Number(match[1]));
    if (citation) {
      parts.push(
        <button
          key={match.index}
          onClick={() => onCitationClick(citation)}
          className="text-primary hover:bg-primary/15 mx-0.5 rounded px-1 align-super text-xs font-medium"
        >
          {match[1]}
        </button>,
      );
    } else {
      parts.push(match[0]);
    }
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < content.length) parts.push(content.slice(lastIndex));
  return parts;
}

/** Flat message blocks, no bubbles — Claude.ai-style. Same visual language as the landing
 * page's ProductPreview mock (Sprout avatar for the tutor, plain right-aligned text for the
 * learner) so the marketing preview and the real thing actually match. */
export function MessageBlock({
  role,
  content,
  streaming = false,
  citations = [],
  onCitationClick,
}: {
  role: string;
  content: string;
  streaming?: boolean;
  citations?: Citation[];
  onCitationClick?: (citation: Citation) => void;
}) {
  if (role === "user") {
    return (
      <div className="flex justify-end">
        <p className="text-body text-base-content/70 max-w-[75%] whitespace-pre-wrap">{content}</p>
      </div>
    );
  }

  return (
    <div className="flex gap-3">
      <span className="bg-primary/15 text-primary flex size-8 shrink-0 items-center justify-center rounded-full">
        <Sprout size={16} />
      </span>
      <p className="text-body text-base-content/90 max-w-[75%] whitespace-pre-wrap">
        {onCitationClick ? renderWithCitations(content, citations, onCitationClick) : content}
        {streaming && <span className="animate-pulse">▍</span>}
      </p>
    </div>
  );
}
