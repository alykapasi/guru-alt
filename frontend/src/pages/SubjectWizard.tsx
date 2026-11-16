import { useState } from "react";
import { AlertCircle, Loader } from "lucide-react";
import { MaterialsStep } from "../components/subject-wizard/MaterialsStep";
import { GoalStep } from "../components/subject-wizard/GoalStep";
import { ReviewStep } from "../components/subject-wizard/ReviewStep";
import { CommitStep } from "../components/subject-wizard/CommitStep";
import type { CurriculumProposal } from "../api/onboarding";
import { useGenerateCurriculum, useGoalSession } from "../api/onboarding";

type WizardStep = "goal" | "materials" | "review" | "commit";

const STEPS: { key: WizardStep; label: string }[] = [
  { key: "goal", label: "Goal" },
  { key: "materials", label: "Materials" },
  { key: "review", label: "Review" },
  { key: "commit", label: "Create" },
];

/** A new subject: the goal first (what the learner wants to learn), then optional materials,
 * then the curriculum built from both. Generation starts on leaving Materials, since it reads
 * the materials chosen there. */
export function SubjectWizard() {
  const [step, setStep] = useState<WizardStep>("goal");
  const [materials, setMaterials] = useState<string[]>([]);
  const { data: sessionId } = useGoalSession();
  const [goal, setGoal] = useState("");
  const [curriculum, setCurriculum] = useState<CurriculumProposal | null>(null);
  const generate = useGenerateCurriculum();

  function handleGoalCommitted(g: string) {
    setGoal(g);
    setStep("materials");
  }

  function build() {
    generate.mutate(
      { goal, sourceIds: materials.length ? materials : null },
      {
        onSuccess: (proposal) => {
          setCurriculum(proposal);
          setStep("review");
        },
      },
    );
  }

  const reached = STEPS.findIndex((s) => s.key === step);

  return (
    <div className="flex flex-col gap-8">
      {/* Progress Indicator */}
      <div className="mx-auto w-full max-w-2xl px-6">
        <ul className="steps w-full" aria-label="Steps">
          {STEPS.map((s, i) => (
            <li key={s.key} className={`step ${i <= reached ? "step-primary" : ""}`}>
              {s.label}
            </li>
          ))}
        </ul>
      </div>

      {/* Step Content */}
      {step === "goal" && (
        <GoalStep
          // Undefined until the server issues it; GoalStep waits rather than sending.
          sessionId={sessionId}
          onGoalCommitted={handleGoalCommitted}
        />
      )}

      {step === "materials" &&
        (generate.isPending ? (
          <div className="mx-auto flex w-full max-w-2xl flex-col items-center justify-center gap-4 px-6 py-16">
            <Loader className="text-primary animate-spin" size={32} />
            <p className="text-body text-base-content/70">Designing your curriculum…</p>
          </div>
        ) : (
          <>
            {generate.isError && (
              <div className="mx-auto w-full max-w-2xl px-6">
                <div className="flex items-start gap-3 rounded-box border border-error bg-error/5 p-4">
                  <AlertCircle size={16} className="text-error shrink-0 mt-0.5" />
                  <p className="text-caption text-error">
                    Couldn't design the curriculum. Press Next to try again.
                  </p>
                </div>
              </div>
            )}
            <MaterialsStep goal={goal} value={materials} onChange={setMaterials} onNext={build} />
          </>
        ))}

      {step === "review" && curriculum && (
        <ReviewStep
          curriculum={curriculum}
          goal={goal}
          sourceIds={materials}
          onChange={setCurriculum}
          onNext={() => setStep("commit")}
          onBack={() => setStep("materials")}
        />
      )}

      {step === "commit" && curriculum && (
        <CommitStep
          curriculum={curriculum}
          sourceIds={materials}
          onBack={() => setStep("review")}
        />
      )}
    </div>
  );
}
