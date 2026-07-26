import { Wrench } from "lucide-react";

/** A transient "working on it" indicator for an in-flight agentic tool call. Not persisted
 * history (app/services/agentic.py only persists the final reply) — this only ever exists
 * during a live stream. */
export function ToolCallChip({ tool }: { tool: string }) {
  return (
    <span className="bg-base-200 text-base-content/60 text-caption inline-flex w-fit items-center gap-1.5 rounded-field px-2.5 py-1">
      <Wrench size={12} />
      {tool}
    </span>
  );
}
