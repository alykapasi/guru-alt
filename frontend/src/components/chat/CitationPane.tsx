import { useEffect, useRef } from "react";
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

/** What a citation shows (S29): the passage, labelled when a re-ingest replaced it, or a plain
 * statement when the passage no longer exists at all. A machine-read passage says so (S27). */
export function CitationBody({
  origin,
  locator,
  text,
  superseded = false,
  missing = false,
  note = null,
}: {
  origin?: string;
  locator?: string | null;
  text?: string;
  superseded?: boolean;
  missing?: boolean;
  note?: string | null;
}) {
  if (missing) {
    return <p className="text-body text-base-content/70">This passage is no longer available.</p>;
  }
  return (
    <>
      <div>
        <p className="text-caption text-base-content/70 truncate">{origin}</p>
        {locator && <p className="text-caption text-primary">{locator}</p>}
        {superseded && (
          <p className="text-caption text-warning">From an earlier version of this source</p>
        )}
        {note && (
          <p className="text-caption text-base-content/70">
            {note.charAt(0).toUpperCase() + note.slice(1)}
          </p>
        )}
      </div>
      <p className="text-body text-base-content/90 whitespace-pre-wrap">{text}</p>
    </>
  );
}

export function CitationPane({ citation, onClose }: { citation: Citation; onClose: () => void }) {
  const { data: chunk, isLoading: chunkLoading, isError: chunkError } = useChunk(citation.chunk_id);
  const { data: source, isLoading: sourceLoading } = useSource(citation.source_id);
  const locator = chunk ? locatorLabel(chunk.provenance) : null;
  const headingRef = useRef<HTMLHeadingElement>(null);

  // Opening a citation is a request to read it: focus goes to the pane, and back to the marker
  // that opened it when the pane closes, so a keyboard user is not dropped at the top of the
  // page (S53). On a narrow screen the sheet's dialog does the same for itself.
  // Per citation, not per mount: clicking a second marker while the pane is open is a new
  // request to read, and closing should return to the marker clicked last. When that marker
  // has since re-rendered away, focus goes to the content rather than to nowhere.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    headingRef.current?.focus();
    return () => {
      const target = opener?.isConnected ? opener : document.getElementById("main");
      target?.focus();
    };
  }, [citation.chunk_id]);

  return (
    <div className="bg-base-100 flex min-h-0 flex-1 flex-col">
      <div className="border-base-300 flex items-center justify-between border-b p-4">
        <h3 ref={headingRef} tabIndex={-1} className="text-h3 flex items-center gap-2 outline-none">
          <FileText size={16} className="text-primary" />
          Source
        </h3>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close source"
          className="hover:bg-base-200 rounded-field p-1.5"
        >
          <X size={16} />
        </button>
      </div>
      <div className="flex flex-1 flex-col gap-3 overflow-y-auto p-4">
        {sourceLoading || chunkLoading ? (
          <p className="text-caption text-base-content/70">Loading…</p>
        ) : (
          <CitationBody
            missing={chunkError || !chunk}
            origin={source?.origin ?? "Your source"}
            locator={locator}
            text={chunk?.text}
            superseded={chunk?.superseded ?? false}
            note={chunk?.reading_note ?? null}
          />
        )}
      </div>
    </div>
  );
}
