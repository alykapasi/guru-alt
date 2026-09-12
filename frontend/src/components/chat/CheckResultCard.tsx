import { CircleCheck, CircleDot, TrendingDown, TrendingUp } from "lucide-react";
import type { CheckComponent, CheckResult } from "../../api/sse";
import { expectedScorePercent, masteryQualifier } from "../../lib/mastery";

/** What happened to an answer the learner gave in conversation (S15).
 *
 * Their answer was already being graded, moving mastery, rescheduling the card and revising
 * the plan — and the only evidence was the tutor's reply, which is not a record. This is the
 * record: what it scored, which components moved, and why it fell short where the grader
 * could actually say.
 *
 * The rule throughout is that absent evidence is shown as absent. A component the grader
 * could not score separately shows no score rather than the item's aggregate (S10), and a
 * component nothing diagnosed shows no reason rather than a plausible one (S09) — because
 * "we could not tell" and "it was fine" are different things to tell somebody about their
 * own work. */

/** The failure kinds, in the learner's terms rather than the grader's vocabulary. Each says
 * what it means for them, since the label alone ("procedural") is jargon at the exact moment
 * somebody is already struggling. */
const FAILURE_COPY: Record<string, string> = {
  notation: "The idea was right — the notation wasn't",
  procedural: "Right method, slip along the way",
  conceptual: "The idea itself needs another look",
  prerequisite: "Something earlier is getting in the way",
};

function Movement({ component }: { component: CheckComponent }) {
  const before = expectedScorePercent(component.prior_ability);
  const after = expectedScorePercent(component.ability);
  const delta = after - before;
  // Below a tenth of a point the arrow would be claiming a direction the estimate does not
  // really have, so it is shown as unchanged instead.
  const moved = Math.abs(delta) >= 0.1;
  const Icon = !moved ? CircleDot : delta > 0 ? TrendingUp : TrendingDown;
  const tone = !moved ? "text-base-content/40" : delta > 0 ? "text-success" : "text-warning";

  return (
    <span className={`text-caption inline-flex items-center gap-1 ${tone}`}>
      <Icon size={13} aria-hidden />
      {moved ? (
        <>
          {Math.round(before)}% → {Math.round(after)}%
        </>
      ) : (
        <>unchanged at {Math.round(after)}%</>
      )}
    </span>
  );
}

export function CheckResultCard({ result }: { result: CheckResult }) {
  return (
    <section
      aria-label="How your answer was graded"
      className="border-base-300 bg-base-200/40 mx-auto flex w-full max-w-3xl flex-col gap-3 rounded-box border px-4 py-3"
    >
      <div className="flex items-center gap-2">
        <CircleCheck
          size={16}
          className={result.correct ? "text-success" : "text-base-content/40"}
          aria-hidden
        />
        <p className="text-body text-base-content/90">
          {result.correct ? "Marked correct" : "Marked — not quite yet"}
        </p>
        <span className="text-caption text-base-content/50 ml-auto">
          scored {Math.round(result.score * 100)}%
        </span>
      </div>

      <ul className="flex flex-col gap-2">
        {result.components.map((component) => (
          <li key={component.kc_id} className="flex flex-col gap-1">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span className="text-caption text-base-content/80">{component.kc_name}</span>
              {component.score !== null && (
                <span className="text-caption text-base-content/50">
                  {Math.round(component.score * 100)}% on this part
                </span>
              )}
              <Movement component={component} />
              <span className="text-caption text-base-content/40">
                {masteryQualifier(component.uncertainty)}
              </span>
            </div>
            {component.failure_kind && (
              <p className="text-caption text-base-content/60">
                {FAILURE_COPY[component.failure_kind] ?? "Worth another look"}
                {component.failure_detail ? ` — ${component.failure_detail}` : ""}
              </p>
            )}
          </li>
        ))}
      </ul>

      <p className="text-caption text-base-content/40">
        Percentages are your expected score on a question of average difficulty, not how much of the
        topic you know.
      </p>
    </section>
  );
}
