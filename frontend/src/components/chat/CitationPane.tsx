import { FileText, X } from "lucide-react";
import { useChunk, useSource } from "../../api/hooks";
import type { Citation } from "../../api/sse";

/** v1 citation click-through: the chunk's own extracted text + locator, not a re-rendered
 * original-format file — works uniformly across every source type with no per-format viewer
 * (see MASTERPLAN §7 "Citation display"). */
function locatorLabel(provenance: Record<string, unknown>): string | null {
  if (typeof provenance.page === "number") return `Page ${provenance.page}`;
  if (typeof provenance.slide === "number") return `Slide ${provenance.slide}`;
  if (typeof provenance.timestamp === "number") return `${Math.round(provenance.timestamp)}s in`;
  if (typeof provenance.paragraph === "number") return `Paragraph ${provenance.paragraph}`;
  return null;
}

export function CitationPane({ citation, onClose }: { citation: Citation; onClose: () => void }) {
  const { data: chunk, isLoading: chunkLoading } = useChunk(citation.chunk_id);
  const { data: source, isLoading: sourceLoading } = useSource(citation.source_id);
  const locator = chunk ? locatorLabel(chunk.provenance) : null;

  return (
    <div className="bg-base-100 flex min-h-0 flex-1 flex-col">
      <div className="border-base-300 flex items-center justify-between border-b p-4">
        <h3 className="text-h3 flex items-center gap-2">
          <FileText size={16} className="text-primary" />
          Source
        </h3>
        <button onClick={onClose} className="hover:bg-base-200 rounded-field p-1.5">
          <X size={16} />
        </button>
      </div>
      <div className="flex flex-1 flex-col gap-3 overflow-y-auto p-4">
        {sourceLoading || chunkLoading ? (
          <p className="text-caption text-base-content/50">Loading…</p>
        ) : (
          <>
            <div>
              <p className="text-caption text-base-content/60 truncate">
                {source?.origin ?? "Unknown source"}
              </p>
              {locator && <p className="text-caption text-primary">{locator}</p>}
            </div>
            <p className="text-body text-base-content/90 whitespace-pre-wrap">{chunk?.text}</p>
          </>
        )}
      </div>
    </div>
  );
}
