import { Sprout } from "lucide-react";
import { RichText } from "../content/RichText";
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
      <div className="text-base-content/90 min-w-0 max-w-[75%]">
        <RichText content={content} citations={citations} onCitationClick={onCitationClick} />
        {streaming && <span className="animate-pulse">▍</span>}
      </div>
    </div>
  );
}
