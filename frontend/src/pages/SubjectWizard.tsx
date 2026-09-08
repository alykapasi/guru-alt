import { useState } from "react";
import { AlertCircle, Loader } from "lucide-react";
import { MaterialsStep } from "../components/subject-wizard/MaterialsStep";
import { GoalStep } from "../components/subject-wizard/GoalStep";
import { ReviewStep } from "../components/subject-wizard/ReviewStep";
import { CommitStep } from "../components/subject-wizard/CommitStep";
import type { CurriculumProposal } from "../api/onboarding";
import { useGenerateCurriculum, useGoalSession } from "../api/onboarding";

type WizardStep = "materials" | "goal" | "review" | "commit";

export function SubjectWizard() {
  const [step, setStep] = useState<WizardStep>("materials");
  const [materials, setMaterials] = useState<string[]>([]);
  const { data: sessionId } = useGoalSession();
  const [goal, setGoal] = useState("");
  const [curriculum, setCurriculum] = useState<CurriculumProposal | null>(null);
  const generate = useGenerateCurriculum();

  function handleGoalCommitted(g: string) {
    setGoal(g);
    generate.mutate(
      { goal: g, sourceIds: materials.length ? materials : null },
      {
        onSuccess: (proposal) => {
          setCurriculum(proposal);
          setStep("review");
        },
      },
    );
  }

  return (
    <div className="flex flex-col gap-8">
      {/* Progress Indicator */}
      <div className="mx-auto w-full max-w-2xl px-6">
        <ul className="steps w-full">
          <li className="step step-primary">Materials</li>
          <li
            className={`step ${step === "goal" || step === "review" || step === "commit" ? "step-primary" : ""}`}
          >
            Goal
          </li>
          <li className={`step ${step === "review" || step === "commit" ? "step-primary" : ""}`}>
            Review
          </li>
          <li className={`step ${step === "commit" ? "step-primary" : ""}`}>Create</li>
        </ul>
      </div>

      {/* Step Content */}
      {step === "materials" && (
        <MaterialsStep value={materials} onChange={setMaterials} onNext={() => setStep("goal")} />
      )}

      {step === "goal" && (
        <>
          {generate.isPending ? (
            <div className="mx-auto flex w-full max-w-2xl flex-col items-center justify-center gap-4 px-6 py-16">
              <Loader className="text-primary animate-spin" size={32} />
              <p className="text-body text-base-content/70">Designing your curriculum…</p>
            </div>
          ) : generate.isError ? (
            <div className="mx-auto w-full max-w-2xl px-6">
              <div className="flex items-start gap-3 rounded-box border border-error bg-error/5 p-4">
                <AlertCircle size={16} className="text-error shrink-0 mt-0.5" />
                <div className="flex flex-col gap-2">
                  <p className="text-caption text-error">
                    Failed to generate curriculum. Please try again.
                  </p>
                  <button
                    onClick={() =>
                      generate.mutate(
                        { goal, sourceIds: materials.length ? materials : null },
                        {
                          onSuccess: (proposal) => {
                            setCurriculum(proposal);
                            setStep("review");
                          },
                        },
                      )
                    }
                    disabled={generate.isPending}
                    className="btn btn-error btn-xs w-fit"
                  >
                    Try again
                  </button>
                </div>
              </div>
            </div>
          ) : (
            <GoalStep
              // Undefined until the server issues it; GoalStep waits rather than sending.
              sessionId={sessionId}
              onGoalCommitted={handleGoalCommitted}
              onBack={() => setStep("materials")}
            />
          )}
        </>
      )}

      {step === "review" && curriculum && (
        <ReviewStep
          curriculum={curriculum}
          goal={goal}
          sourceIds={materials}
          onChange={setCurriculum}
          onNext={() => setStep("commit")}
          onBack={() => setStep("goal")}
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
