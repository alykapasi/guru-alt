import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useChatConversation } from "../hooks/useChatConversation";
import { usePracticeAction } from "../api/hooks";
import { MessageList } from "../components/chat/MessageList";
import { Composer } from "../components/chat/Composer";
import { ItemPanel } from "../components/lessons/ItemPanel";
import { CitationPane } from "../components/chat/CitationPane";
import { PracticeControls } from "../components/chat/PracticeControls";
import { PracticePausedStrip } from "../components/chat/PracticePausedStrip";
import { RATINGS } from "../lib/flashcardRatings";
import type { Citation } from "../api/sse";

const PRACTICE_ENDED_NOTICE = "That question no longer fits your plan, so practice ended.";

/** A guided-practice session: the workflow-mode chat transcript plus a persistent side panel
 * for the item being practiced (see docs/ROADMAP.md Phase 7's design brief). Reuses the same
 * conversation/SSE machinery as the plain chat page — a session is just a conversation whose
 * turns are always sent with mode "workflow". */
export function Session() {
  const { conversationId } = useParams<{ conversationId: string }>();
  const {
    messages,
    isLoadingMessages,
    hasEarlierMessages,
    isLoadingEarlier,
    loadEarlierMessages,
    pending,
    error,
    canRetry,
    retry,
    item,
    sessionDetail,
    awaitingReply,
    practicePaused,
    applyPracticeState,
    send,
  } = useChatConversation(conversationId);
  const practiceAction = usePracticeAction(conversationId);
  const hasStartedRef = useRef(false);
  const [citation, setCitation] = useState<Citation | null>(null);
  // Set only when a resume finds the paused question has gone stale (S52) — the one way
  // practice can end outside the workflow's own mastered/capped outcomes. Local, not derived
  // from `sessionDetail`: the strip that shows it must keep showing it after the phase has
  // already moved back to "chatting".
  const [endedNotice, setEndedNotice] = useState<string | null>(null);
  // A skip ends the session the same way mastered/capped do (composer disabled, controls
  // gone), but leaves no `sessionDetail` a DETAIL_COPY lookup would recognize — it is neither
  // an outcome nor an error, just the learner choosing not to answer.
  const [skipped, setSkipped] = useState(false);

  useEffect(() => {
    if (isLoadingMessages || pending || hasStartedRef.current) return;
    if (messages.length === 0) {
      hasStartedRef.current = true;
      send("Let's begin.", { mode: "workflow" });
    }
  }, [isLoadingMessages, messages.length, pending, send]);

  // "practice" is mid-session (the learner should keep answering); "mastered"/"capped" are the
  // two ways a workflow run ends on its own (see app/services/workflow.py::run_workflow_turn);
  // a stale resume and an explicit skip are the two the learner's own controls can cause (S52).
  const ended =
    sessionDetail === "mastered" || sessionDetail === "capped" || endedNotice !== null || skipped;
  const practiceBusy = practiceAction.isPending || !!pending;

  // A flashcard's rating rides the same turn request every other reply does (ChatTurnRequest.
  // rating) — no second request path. `content` still carries the label so the transcript
  // reads as what the learner did, rather than a bare digit nobody typed.
  //
  // Which is also why the buttons go dead while a turn is in flight: `send` early-returns on
  // `pending` (useChatConversation), so a click landing then is silently dropped — the same
  // reason the Composer is disabled, and the same treatment.
  function handleRate(rating: number) {
    const label = RATINGS.find((r) => r.value === rating)?.label ?? String(rating);
    void send(label, { mode: "workflow", rating });
  }

  function handlePause() {
    practiceAction.mutate("pause");
  }

  // A resume that finds the question gone is not an error (S52): the plan moved on, or the
  // learner mastered it elsewhere, while the pause held. Either way there is nothing left to
  // hand back, so the session ends the same as a skip would, but says why.
  function handleResume() {
    practiceAction.mutate("resume", {
      onSuccess: (state) => {
        if (state.ended) setEndedNotice(PRACTICE_ENDED_NOTICE);
        applyPracticeState(state);
      },
    });
  }

  function handleSkip() {
    practiceAction.mutate("skip", {
      onSuccess: (state) => {
        setSkipped(true);
        applyPracticeState(state);
      },
    });
  }

  return (
    <div className="flex min-h-0 flex-1">
      <div className="flex min-h-0 flex-1 flex-col">
        {isLoadingMessages ? (
          <div className="flex flex-1 items-center justify-center">
            <p className="text-caption text-base-content/50">Loading session…</p>
          </div>
        ) : (
          <MessageList
            messages={messages}
            pending={pending}
            goal={null}
            awaitingGoalAccept={false}
            onAcceptGoal={() => {}}
            onCitationClick={setCitation}
            hasEarlier={hasEarlierMessages}
            isLoadingEarlier={isLoadingEarlier}
            onLoadEarlier={loadEarlierMessages}
          />
        )}
        {error && (
          <div className="text-caption text-error mx-auto flex w-full max-w-3xl items-center gap-3 px-6 pb-2">
            <p>{error}</p>
            {canRetry && (
              <button type="button" className="btn btn-ghost btn-xs" onClick={() => void retry()}>
                Try again
              </button>
            )}
          </div>
        )}
        {endedNotice ? (
          <PracticePausedStrip onResume={handleResume} onSkip={handleSkip} notice={endedNotice} />
        ) : practicePaused ? (
          <PracticePausedStrip
            onResume={handleResume}
            onSkip={handleSkip}
            disabled={practiceBusy}
          />
        ) : (
          awaitingReply &&
          !ended && (
            <PracticeControls onPause={handlePause} onSkip={handleSkip} disabled={practiceBusy} />
          )
        )}
        <Composer
          disabled={!!pending || ended}
          onSend={(content) => send(content, { mode: "workflow" })}
          fixedMode="workflow"
          placeholder={practicePaused ? "Ask anything…" : "Your answer…"}
        />
      </div>
      {/* Evidence stacks above the question rather than replacing it: a learner opening a
          citation is checking a source *in order to answer*, so hiding the item they are
          answering to show it would defeat the click. */}
      <aside className="border-base-300 divide-base-300 flex w-80 shrink-0 flex-col divide-y border-l">
        {citation && <CitationPane citation={citation} onClose={() => setCitation(null)} />}
        <ItemPanel
          item={item}
          detail={sessionDetail}
          onRate={handleRate}
          // While paused a rating would go to the tutor, not the grader (spec §4.2), so the
          // learner must go back to the question before rating it.
          ratingDisabled={!!pending || practicePaused}
        />
      </aside>
    </div>
  );
}
