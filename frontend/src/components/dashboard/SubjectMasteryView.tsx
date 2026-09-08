import { useSubjectMastery } from "../../api/hooks";
import type { components } from "../../api/schema";
import { coverageLabel, expectedScorePercent, masteryQualifier } from "../../lib/mastery";
import { MasteryRing } from "./MasteryRing";

type TopicMastery = components["schemas"]["TopicMasteryRead"];

function TopicMasteryRow({ topic }: { topic: TopicMastery }) {
  const assessed = topic.assessed_kcs > 0;
  const percent = assessed ? expectedScorePercent(topic.ability) : 0;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <p className="text-body">{topic.topic_name}</p>
        <p className="text-caption text-base-content/60">
          {assessed
            ? `${Math.round(percent)}% (${masteryQualifier(topic.uncertainty)})`
            : "not assessed"}
        </p>
      </div>
      <div className="bg-base-300 h-2 w-full overflow-hidden rounded-full">
        <div className="bg-primary h-full rounded-full" style={{ width: `${percent}%` }} />
      </div>
      <p className="text-caption text-base-content/50 pl-4">
        {coverageLabel(topic.assessed_kcs, topic.total_kcs)}
      </p>
      <div className="flex flex-col gap-1 pl-4">
        {topic.kcs.map((kc) => (
          <div key={kc.kc_id} className="flex items-center justify-between">
            <span className="text-caption text-base-content/70">{kc.kc_name}</span>
            <span className="text-caption text-base-content/50">
              {kc.assessed
                ? `${Math.round(expectedScorePercent(kc.ability))}%${kc.mastered ? " · mastered" : ""}`
                : "not assessed"}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Subject → topic → KC drill-down (TECHNICAL_DESIGN §7.4: "Calculus 62% (wide) ... Integrals
 * 40%, integration-by-parts weakest").
 *
 * Percentages are expected score on an average question, and components with no evidence
 * behind them say so rather than showing the prior's 50%.
 */
export function SubjectMasteryView({ subjectId }: { subjectId: string }) {
  const { data } = useSubjectMastery(subjectId);
  if (!data) {
    return <p className="text-caption text-base-content/50">Loading mastery…</p>;
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-6">
        <MasteryRing
          ability={data.ability}
          uncertainty={data.uncertainty}
          assessed={data.assessed_kcs > 0}
        />
        <div>
          <p className="text-h3">Expected score</p>
          <p className="text-caption text-base-content/60">
            On a question of average difficulty — not the share of the subject covered.
          </p>
          <p className="text-caption text-base-content/60">
            {data.topics.length} topic{data.topics.length === 1 ? "" : "s"} ·{" "}
            {coverageLabel(data.assessed_kcs, data.total_kcs)}
          </p>
        </div>
      </div>
      <div className="flex flex-col gap-4">
        {data.topics.length === 0 ? (
          <p className="text-caption text-base-content/50">No topics yet in this subject.</p>
        ) : (
          data.topics.map((topic) => <TopicMasteryRow key={topic.topic_id} topic={topic} />)
        )}
      </div>
    </div>
  );
}
