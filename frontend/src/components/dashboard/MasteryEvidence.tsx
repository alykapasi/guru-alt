import type { components } from "../../api/schema";

type KCMastery = components["schemas"]["KCMasteryRead"];

/** What a component's estimate actually rests on (S14).
 *
 * The same percentage can come from four different problems solved unaided over weeks, or
 * from one question answered four times in ten minutes. Those are not the same claim, and a
 * number shown alone invites the stronger reading — which is the one the dashboard was making
 * by default. The backend has carried these counts since S14; nothing displayed them.
 *
 * Absence is stated rather than left out. "No delayed check yet" and silence look identical
 * on screen, and only one of them is true. */
export function MasteryEvidence({ kc }: { kc: KCMastery }) {
  if (!kc.assessed) return null;

  const problems = `${kc.distinct_items} ${kc.distinct_items === 1 ? "problem" : "problems"}`;
  const unassisted = `${kc.unassisted_items} unaided`;

  return (
    <p className="text-caption text-base-content/40 pl-4">
      {problems}, {unassisted}
      {" · "}
      <span className={kc.transfer_shown ? "text-success" : undefined}>
        {kc.transfer_shown ? "solved a different one" : "no different problem yet"}
      </span>
      {" · "}
      <span className={kc.retention_shown ? "text-success" : undefined}>
        {kc.retention_shown ? "held up later" : "no delayed check yet"}
      </span>
    </p>
  );
}
