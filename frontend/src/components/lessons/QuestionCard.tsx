import { RichText } from "../content/RichText";
import type { ItemEvent } from "../../api/sse";

/** The question, kept in view above the composer on a narrow screen (S53). A learner answering
 * must be able to see what they are answering; the full panel, with its rating controls, is one
 * tap away rather than beside the transcript where it no longer fits. */
export function QuestionCard({ item, onShow }: { item: ItemEvent | null; onShow: () => void }) {
  return (
    <div className="border-base-300 bg-base-200/40 flex items-start gap-3 border-t px-4 py-3">
      <div className="line-clamp-3 min-w-0 flex-1">
        {item ? (
          <RichText content={item.stem} className="text-base-content/90" />
        ) : (
          <p className="text-caption text-base-content/50">Preparing your practice…</p>
        )}
      </div>
      <button type="button" className="btn btn-ghost btn-xs shrink-0" onClick={onShow}>
        Show question
      </button>
    </div>
  );
}
