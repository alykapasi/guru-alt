import { useState } from "react";
import { Archive, ArchiveRestore, FileText, Link2, Trash2 } from "lucide-react";
import { useArchive, useRetrySource } from "../../api/hooks";
import type { components } from "../../api/schema";
import { RemovalDialog } from "../removal/RemovalDialog";
import { duplicateLabel } from "./duplicateLabel";
import { SourceActions } from "./SourceActions";

type Source = components["schemas"]["SourceRead"];

const STATUS_BADGE: Record<string, string> = {
  pending: "badge-ghost",
  processing: "badge-info",
  done: "badge-success",
  failed: "badge-error",
};

function SourceRow({
  source,
  sources,
  archived,
}: {
  source: Source;
  sources: Source[];
  archived: boolean;
}) {
  const Icon = source.kind === "url" ? Link2 : FileText;
  const retry = useRetrySource();
  const archive = useArchive("source");
  const [removing, setRemoving] = useState(false);
  const label = duplicateLabel(source, sources);
  return (
    <div className="hover:bg-base-200 flex items-center justify-between gap-4 rounded-field px-3 py-2">
      <div className="flex min-w-0 items-center gap-2">
        <Icon size={16} className="text-base-content/70 shrink-0" />
        <div className="flex min-w-0 flex-col">
          <span className="text-body truncate" title={source.error ?? undefined}>
            {source.origin}
          </span>
          {label && <span className="text-caption text-base-content/70">{label}</span>}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {!archived && (
          <SourceActions
            status={source.status}
            kind={source.kind}
            pending={retry.isPending}
            error={retry.error}
            onRetry={(confirm) => retry.mutate({ sourceId: source.id, confirm })}
          />
        )}
        <span className={`badge badge-sm shrink-0 ${STATUS_BADGE[source.status] ?? "badge-ghost"}`}>
          {source.status}
        </span>
        <button
          type="button"
          className="btn btn-ghost btn-xs"
          disabled={archive.isPending}
          onClick={() => archive.mutate({ id: source.id, archived: !archived })}
          aria-label={archived ? `Unarchive ${source.origin}` : `Archive ${source.origin}`}
          title={archived ? "Unarchive" : "Archive — keep it, but stop using it"}
        >
          {archived ? <ArchiveRestore size={14} /> : <Archive size={14} />}
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-xs text-error"
          onClick={() => setRemoving(true)}
          aria-label={`Delete ${source.origin}`}
          title="Delete…"
        >
          <Trash2 size={14} />
        </button>
        {removing && (
          <RemovalDialog
            kind="source"
            id={source.id}
            name={source.origin}
            open
            onClose={() => setRemoving(false)}
          />
        )}
      </div>
    </div>
  );
}

export function SourceList({
  sources,
  isLoading,
  archived = false,
}: {
  sources: Source[];
  isLoading: boolean;
  /** Rows of the Archived section: Unarchive instead of Archive, and no re-processing (S61). */
  archived?: boolean;
}) {
  if (isLoading) {
    return <p className="text-caption text-base-content/70">Loading…</p>;
  }
  if (sources.length === 0) {
    return <p className="text-caption text-base-content/70">No materials yet.</p>;
  }
  return (
    <div className="flex flex-col gap-1">
      {sources.map((s) => (
        <SourceRow key={s.id} source={s} sources={sources} archived={archived} />
      ))}
    </div>
  );
}
