import { Sprout } from "lucide-react";
import { RichText } from "../content/RichText";
import { CoverageChip, type Coverage } from "./CoverageChip";
import type { Citation } from "../../api/sse";

/** Flat message blocks, no bubbles — Claude.ai-style. Same visual language as the landing
 * page's ProductPreview mock (Sprout avatar for the tutor, plain right-aligned text for the
 * learner) so the marketing preview and the real thing actually match.
 *
 * The tutor's side goes through the shared renderer, so a derivation, a table, or a code
 * sample arrives as the thing it is rather than as its source syntax (S53). The learner's own
 * message deliberately does not: showing someone's message back to them with their asterisks
 * turned into emphasis means they can no longer see what they actually sent.
 */
export function MessageBlock({
  role,
  content,
  streaming = false,
  adminAttributed = false,
  citations = [],
  onCitationClick,
  coverage = null,
  sourceCount = 0,
}: {
  role: string;
  content: string;
  streaming?: boolean;
  adminAttributed?: boolean;
  citations?: Citation[];
  onCitationClick?: (citation: Citation) => void;
  coverage?: Coverage | null;
  sourceCount?: number;
}) {
  if (role === "user") {
    return (
      <div className="flex flex-col items-end gap-1">
        {adminAttributed && <span className="text-caption text-base-content/50">Admin</span>}
        <p className="text-body text-base-content/70 max-w-[75%] whitespace-pre-wrap">{content}</p>
      </div>
    );
  }

  return (
    <div className="flex gap-3">
      <span className="bg-primary/15 text-primary flex size-8 shrink-0 items-center justify-center rounded-full">
        <Sprout size={16} />
      </span>
      <div className="text-base-content/90 min-w-0 max-w-[75%]">
        {adminAttributed && (
          <span className="text-caption text-base-content/50">Reply to admin</span>
        )}
        <RichText content={content} citations={citations} onCitationClick={onCitationClick} />
        {streaming && <span className="animate-pulse">▍</span>}
        {!streaming && <CoverageChip coverage={coverage} sourceCount={sourceCount} />}
      </div>
    </div>
  );
}
