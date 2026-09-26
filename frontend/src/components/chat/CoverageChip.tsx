import { BookOpen } from "lucide-react";

export type Coverage = "cited" | "retrieved_not_cited" | "none";

/** How much of a reply the learner's sources carried (S28). The server derives it from what
 * was offered and what was cited, never from the model's own account; "partly" is left to the
 * tutor's words because only the text can say it. Null — a General chat, or a reply from
 * before this was recorded — shows nothing rather than guessing. */
export function CoverageChip({
  coverage,
  sourceCount,
}: {
  coverage: Coverage | null | undefined;
  sourceCount: number;
}) {
  if (!coverage) return null;
  const label =
    coverage === "cited"
      ? sourceCount === 1
        ? "Draws on one of your sources"
        : `Draws on ${sourceCount} of your sources`
      : coverage === "retrieved_not_cited"
        ? "Your materials were searched but not used"
        : "Not from your materials";
  return (
    <span className="text-caption text-base-content/60 flex items-center gap-1">
      <BookOpen size={12} />
      {label}
    </span>
  );
}
