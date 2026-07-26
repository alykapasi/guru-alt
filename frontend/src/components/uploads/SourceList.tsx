import { FileText, Link2 } from "lucide-react";
import type { components } from "../../api/schema";

type Source = components["schemas"]["SourceRead"];

const STATUS_BADGE: Record<string, string> = {
  pending: "badge-ghost",
  processing: "badge-info",
  done: "badge-success",
  failed: "badge-error",
};

function SourceRow({ source }: { source: Source }) {
  const Icon = source.kind === "url" ? Link2 : FileText;
  return (
    <div className="hover:bg-base-200 flex items-center justify-between gap-4 rounded-field px-3 py-2">
      <div className="flex min-w-0 items-center gap-2">
        <Icon size={16} className="text-base-content/50 shrink-0" />
        <span className="text-body truncate" title={source.error ?? undefined}>
          {source.origin}
        </span>
      </div>
      <span className={`badge badge-sm shrink-0 ${STATUS_BADGE[source.status] ?? "badge-ghost"}`}>
        {source.status}
      </span>
    </div>
  );
}

export function SourceList({ sources, isLoading }: { sources: Source[]; isLoading: boolean }) {
  if (isLoading) {
    return <p className="text-caption text-base-content/50">Loading…</p>;
  }
  if (sources.length === 0) {
    return <p className="text-caption text-base-content/50">No materials yet.</p>;
  }
  return (
    <div className="flex flex-col gap-1">
      {sources.map((s) => (
        <SourceRow key={s.id} source={s} />
      ))}
    </div>
  );
}
