import type { components } from "../../api/schema";

type Source = components["schemas"]["SourceRead"];

/** A text duplicate is stored without passages of its own; say whose text it shares (S77)
 * rather than letting it look like a finished source with nothing in it. */
export function duplicateLabel(source: Source, sources: Source[]): string | null {
  if (!source.duplicate_of_id) return null;
  const original = sources.find((s) => s.id === source.duplicate_of_id);
  return original ? `Same text as ${original.origin}` : "Same text as another of your sources";
}
