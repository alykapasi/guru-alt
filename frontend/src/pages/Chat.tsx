import { useState } from "react";
import { Navigate, useParams } from "react-router-dom";
import { useChatConversation } from "../hooks/useChatConversation";
import { usePracticeAction } from "../api/hooks";
import { MessageList } from "../components/chat/MessageList";
import { Composer } from "../components/chat/Composer";
import { CitationPane } from "../components/chat/CitationPane";
import { PracticeControls } from "../components/chat/PracticeControls";
import { PracticePausedStrip } from "../components/chat/PracticePausedStrip";
import type { Citation } from "../api/sse";

export function Chat() {
  const { conversationId } = useParams<{ conversationId: string }>();
  const {
    conversation,
    messages,
    isLoadingMessages,
    hasEarlierMessages,
    isLoadingEarlier,
    loadEarlierMessages,
    pending,
    error,
    canRetry,
    retry,
    awaitingGoalAccept,
    awaitingReply,
    practicePaused,
    applyPracticeState,
    send,
  } = useChatConversation(conversationId);
  const practiceAction = usePracticeAction(conversationId);
  const practiceBusy = practiceAction.isPending || !!pending;
  const [citation, setCitation] = useState<Citation | null>(null);

  // A tutor check (S15) is already conversational — declining it is just another message, and
  // only Skip is offered here; Pause is a guided-practice-session concept (see Session.tsx).
  function handleSkip() {
    practiceAction.mutate("skip", { onSuccess: applyPracticeState });
  }

  function handleResume() {
    practiceAction.mutate("resume", { onSuccess: applyPracticeState });
  }

  // A session conversation's paused workflow reply must never be shown behind the plain
  // chat gate (e.g. a stale bookmark or the browser back button landing here directly).
  if (conversation?.kind === "session") {
    return <Navigate to={`/app/lessons/session/${conversation.id}`} replace />;
  }

  return (
    <div className="flex min-h-0 flex-1">
      <div className="flex min-h-0 flex-1 flex-col">
        {isLoadingMessages ? (
          <div className="flex flex-1 items-center justify-center">
            <p className="text-caption text-base-content/50">Loading conversation…</p>
          </div>
        ) : (
          <MessageList
            messages={messages}
            pending={pending}
            goal={conversation?.goal}
            awaitingGoalAccept={awaitingGoalAccept}
            onAcceptGoal={() =>
              send("Sounds good, let's get started.", { mode: "chat", satisfied: true })
            }
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
        {practicePaused ? (
          <PracticePausedStrip
            onResume={handleResume}
            onSkip={handleSkip}
            disabled={practiceBusy}
          />
        ) : (
          awaitingReply && <PracticeControls onSkip={handleSkip} disabled={practiceBusy} />
        )}
        <Composer disabled={!!pending} onSend={(content, mode) => send(content, { mode })} />
      </div>
      {citation && (
        <aside className="border-base-300 flex w-80 shrink-0 flex-col border-l">
          <CitationPane citation={citation} onClose={() => setCitation(null)} />
        </aside>
      )}
    </div>
  );
}
