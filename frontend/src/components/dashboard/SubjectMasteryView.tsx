import { useSubjectMastery } from "../../api/hooks";
import type { components } from "../../api/schema";
import { masteryPercent, masteryQualifier } from "../../lib/mastery";
import { MasteryRing } from "./MasteryRing";

type TopicMastery = components["schemas"]["TopicMasteryRead"];

function TopicMasteryRow({ topic }: { topic: TopicMastery }) {
  const percent = masteryPercent(topic.ability);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <p className="text-body">{topic.topic_name}</p>
        <p className="text-caption text-base-content/60">
          {Math.round(percent)}% ({masteryQualifier(topic.uncertainty)})
        </p>
      </div>
      <div className="bg-base-300 h-2 w-full overflow-hidden rounded-full">
        <div className="bg-primary h-full rounded-full" style={{ width: `${percent}%` }} />
      </div>
      <div className="flex flex-col gap-1 pl-4">
        {topic.kcs.map((kc) => (
          <div key={kc.kc_id} className="flex items-center justify-between">
            <span className="text-caption text-base-content/70">{kc.kc_name}</span>
            <span className="text-caption text-base-content/50">
              {Math.round(masteryPercent(kc.ability))}%{kc.mastered ? " · mastered" : ""}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Subject → topic → KC mastery drill-down (TECHNICAL_DESIGN §7.4: "Calculus 62% (wide) ...
 * Integrals 40%, integration-by-parts weakest"). */
export function SubjectMasteryView({ subjectId }: { subjectId: string }) {
  const { data } = useSubjectMastery(subjectId);
  if (!data) {
    return <p className="text-caption text-base-content/50">Loading mastery…</p>;
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-6">
        <MasteryRing ability={data.ability} uncertainty={data.uncertainty} />
        <div>
          <p className="text-h3">Overall mastery</p>
          <p className="text-caption text-base-content/60">
            {data.topics.length} topic{data.topics.length === 1 ? "" : "s"}
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
