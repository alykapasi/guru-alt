import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Play } from "lucide-react";
import {
  useCreateConversation,
  useGenerateLessonPlan,
  useKC,
  useLessonPlan,
  usePlacementPrompt,
  useSubmitPlacement,
} from "../../api/hooks";
import type { components } from "../../api/schema";
import { LessonStepRow } from "./LessonStepRow";

type PlacementResult = components["schemas"]["PlacementResultRead"];

/** Shown once per subject, before a lesson plan exists. Placement is optional — it just seeds
 * better initial per-KC levels — so "Generate lesson plan" is always available even if the
 * learner skips straight past it. */
function NoPlanCard({ subjectId }: { subjectId: string }) {
  const [placementOpen, setPlacementOpen] = useState(false);
  const [placementResult, setPlacementResult] = useState<PlacementResult | null>(null);
  const { data: prompt } = usePlacementPrompt(placementOpen ? subjectId : undefined);
  const [background, setBackground] = useState("");
  const submitPlacement = useSubmitPlacement(subjectId);
  const [goal, setGoal] = useState("");
  const generatePlan = useGenerateLessonPlan(subjectId);

  function submitBackground() {
    submitPlacement.mutate(background, { onSuccess: setPlacementResult });
  }

  return (
    <div className="border-base-300 flex flex-col gap-5 rounded-box border p-6">
      <p className="text-body text-base-content/70">No lesson plan yet for this subject.</p>

      {placementResult ? (
        <p className="text-caption text-primary">
          Thanks — we've set initial levels for {placementResult.seeded.length} knowledge component
          {placementResult.seeded.length === 1 ? "" : "s"} based on your background.
        </p>
      ) : placementOpen ? (
        <div className="flex flex-col gap-2">
          <p className="text-caption text-base-content/60">
            {prompt?.question ?? "Tell us about your background with this subject."}
          </p>
          <textarea
            value={background}
            onChange={(e) => setBackground(e.target.value)}
            rows={3}
            placeholder="I've worked through…"
            className="textarea text-body"
          />
          <button
            onClick={submitBackground}
            disabled={!background.trim() || submitPlacement.isPending}
            className="btn btn-outline btn-sm w-fit"
          >
            Submit background
          </button>
        </div>
      ) : (
        <button
          onClick={() => setPlacementOpen(true)}
          className="text-caption text-primary w-fit hover:underline"
        >
          Tell us your background first — optional, helps the plan start at the right level
        </button>
      )}

      <div className="flex items-center gap-2">
        <input
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          placeholder="What do you want to focus on? (optional)"
          className="input text-body flex-1"
        />
        <button
          onClick={() => generatePlan.mutate(goal.trim() || null)}
          disabled={generatePlan.isPending}
          className="btn btn-primary btn-sm shrink-0"
        >
          Generate lesson plan
        </button>
      </div>
    </div>
  );
}

export function LessonPlanPanel({ subjectId }: { subjectId: string }) {
  const { data: plan, isLoading } = useLessonPlan(subjectId);
  const navigate = useNavigate();
  const createConversation = useCreateConversation();
  const activeStep = plan?.steps.find((s) => s.status === "active");
  const { data: activeKC } = useKC(activeStep?.kc_id);

  if (isLoading) {
    return <p className="text-caption text-base-content/50">Loading plan…</p>;
  }
  if (!plan) {
    return <NoPlanCard subjectId={subjectId} />;
  }

  function startPractice() {
    createConversation.mutate(
      {
        subject_id: subjectId,
        kind: "session",
        // Session conversations otherwise persist with no title/goal — labels them sensibly
        // in the conversation history sidebar instead of falling back to "New conversation".
        title: activeKC ? `Practice: ${activeKC.name}` : undefined,
      },
      { onSuccess: (created) => navigate(`/app/lessons/session/${created.id}`) },
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between gap-4">
        <p className="text-caption text-base-content/60">
          {plan.goal ? `Goal: ${plan.goal}` : "No specific goal set."}
        </p>
        {activeStep ? (
          <button
            onClick={startPractice}
            disabled={createConversation.isPending}
            className="btn btn-primary btn-sm shrink-0"
          >
            <Play size={14} />
            Start practice
          </button>
        ) : (
          <p className="text-caption text-base-content/50 shrink-0">
            Nothing due right now — nice work.
          </p>
        )}
      </div>
      <div className="flex flex-col gap-1">
        {plan.steps.map((step) => (
          <LessonStepRow key={step.order} step={step} />
        ))}
      </div>
    </div>
  );
}
