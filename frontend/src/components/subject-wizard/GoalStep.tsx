import { useState, useEffect } from "react";
import { Send, Sprout, AlertCircle } from "lucide-react";
import { useGoalRefinement } from "../../api/onboarding";

export interface GoalStepProps {
  /** Undefined while the server-issued session id is still in flight. */
  sessionId: string | undefined;
  onGoalCommitted: (goal: string) => void;
  onBack: () => void;
}

export function GoalStep({ sessionId, onGoalCommitted, onBack }: GoalStepProps) {
  const { proposal, committedGoal, isStreaming, error, tokens, send } =
    useGoalRefinement(sessionId);
  const [input, setInput] = useState("");

  // Watch committedGoal and trigger callback
  useEffect(() => {
    if (committedGoal) {
      onGoalCommitted(committedGoal);
    }
  }, [committedGoal, onGoalCommitted]);

  async function handleSend() {
    if (!input.trim() || isStreaming) return;
    await send(input, false);
    setInput("");
  }

  async function handleLooksGood() {
    if (!proposal || isStreaming) return;
    await send("", true);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6 px-6 py-8">
      <div className="flex flex-col gap-2">
        <h2 className="text-h2">What's your learning goal?</h2>
        <p className="text-caption text-base-content/60">
          Tell us what you'd like to learn. We'll refine it together.
        </p>
      </div>

      <div className="flex flex-col gap-4">
        {/* Proposal from the LLM */}
        {proposal && (
          <div className="flex gap-3">
            <span className="bg-primary/15 text-primary flex size-8 shrink-0 items-center justify-center rounded-full">
              <Sprout size={16} />
            </span>
            <p className="text-body text-base-content/90 max-w-[75%] whitespace-pre-wrap">
              {proposal}
              {isStreaming && <span className="animate-pulse">▍</span>}
            </p>
          </div>
        )}

        {/* Streaming tokens */}
        {isStreaming && tokens && !proposal && (
          <div className="flex gap-3">
            <span className="bg-primary/15 text-primary flex size-8 shrink-0 items-center justify-center rounded-full">
              <Sprout size={16} />
            </span>
            <p className="text-body text-base-content/90 max-w-[75%] whitespace-pre-wrap">
              {tokens}
              <span className="animate-pulse">▍</span>
            </p>
          </div>
        )}

        {/* Error state */}
        {error && (
          <div className="flex items-start gap-3 rounded-box border border-error bg-error/5 p-4">
            <AlertCircle size={16} className="text-error shrink-0 mt-0.5" />
            <div className="flex flex-col gap-2">
              <p className="text-caption text-error">{error}</p>
              <button
                onClick={() => handleSend()}
                disabled={isStreaming || !sessionId}
                className="btn btn-error btn-xs w-fit"
              >
                Try again
              </button>
            </div>
          </div>
        )}

        {/* Initial prompt if no proposal yet */}
        {!proposal && !error && !isStreaming && (
          <div className="flex justify-end">
            <p className="text-caption text-base-content/50">Share your goal to get started.</p>
          </div>
        )}
      </div>

      {/* Input and controls */}
      <div className="flex flex-col gap-3">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="E.g., 'Learn Python for data analysis' or 'Improve my algebra skills'…"
          className="textarea textarea-bordered text-body resize-none"
          rows={3}
          disabled={isStreaming || !!committedGoal || !sessionId}
        />

        <div className="flex justify-between gap-3">
          <button onClick={onBack} className="btn btn-ghost">
            Back
          </button>

          <div className="flex gap-3">
            {proposal && !committedGoal && (
              <button
                onClick={handleLooksGood}
                disabled={isStreaming || !proposal || !sessionId}
                className="btn btn-primary"
              >
                Looks good
              </button>
            )}

            {!committedGoal && (
              <button
                onClick={handleSend}
                disabled={!input.trim() || isStreaming || !sessionId}
                className="btn btn-outline btn-primary gap-1"
              >
                <Send size={16} />
                Send
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
