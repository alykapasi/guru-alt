import { AlertTriangle } from "lucide-react";
import { usePrerequisiteConflicts, useRemovePrerequisite } from "../../api/hooks";

/** Prerequisite cycles in this subject, shown to its owner with a way out (S23).
 *
 * A cycle means two components each claim to come first. The planner already copes by
 * ignoring one edge of the ring; this says which, so the plan's order is explained, and offers
 * to remove that edge for good. Which claim is wrong is the owner's call — the graph cannot
 * know — so nothing is removed without them. Renders nothing when there is nothing wrong. */
export function CurriculumIssuesPanel({ subjectId }: { subjectId: string }) {
  const { data: conflicts } = usePrerequisiteConflicts(subjectId);
  const remove = useRemovePrerequisite(subjectId);
  if (!conflicts || conflicts.length === 0) return null;
  return (
    <section className="flex max-w-2xl flex-col gap-2" aria-label="Curriculum issues">
      <h2 className="text-h3 flex items-center gap-2">
        <AlertTriangle size={16} className="text-warning" /> Curriculum issues
      </h2>
      {conflicts.map((c) => (
        <div
          key={`${c.prereq_kc_id}-${c.kc_id}`}
          className="rounded-field bg-warning/10 flex items-center justify-between gap-3 px-3 py-2"
        >
          <p className="text-body">
            {c.kc_name} requires {c.prereq_name}, but {c.prereq_name} already depends on {c.kc_name}
            . The plan is ignoring this prerequisite for now.
          </p>
          <button
            type="button"
            className="btn btn-ghost btn-xs shrink-0"
            disabled={remove.isPending}
            onClick={() => remove.mutate({ kcId: c.kc_id, prereqKcId: c.prereq_kc_id })}
          >
            Remove this prerequisite
          </button>
        </div>
      ))}
    </section>
  );
}
