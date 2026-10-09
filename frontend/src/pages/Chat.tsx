import { useState } from "react";
import { Navigate, useParams } from "react-router-dom";
import { useChatConversation } from "../hooks/useChatConversation";
import { useArchive, usePracticeAction } from "../api/hooks";
import { MessageList } from "../components/chat/MessageList";
import { Composer } from "../components/chat/Composer";
import { CitationPane } from "../components/chat/CitationPane";
import { PracticeControls } from "../components/chat/PracticeControls";
import { PracticePausedStrip } from "../components/chat/PracticePausedStrip";
import type { Citation } from "../api/sse";
import { TurnError } from "../components/chat/TurnError";
import { SidePanel } from "../components/layout/SidePanel";
import { LiveAnnouncer } from "../components/LiveAnnouncer";
import { useTurnAnnouncement } from "../hooks/useTurnAnnouncement";

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
    retryAfter,
    retry,
    awaitingGoalAccept,
    awaitingReply,
    practicePaused,
    applyPracticeState,
    send,
    stop,
  } = useChatConversation(conversationId);
  const practiceAction = usePracticeAction(conversationId);
  const unarchive = useArchive("conversation");
  const practiceBusy = practiceAction.isPending || !!pending;
  const [citation, setCitation] = useState<Citation | null>(null);
  const announcement = useTurnAnnouncement(!!pending, error);

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
    <div className="flex min-h-0 min-w-0 flex-1">
      <main id="main" tabIndex={-1} className="outline-none flex min-h-0 min-w-0 flex-1 flex-col">
        <LiveAnnouncer message={announcement} />
        {isLoadingMessages ? (
          <div className="flex flex-1 items-center justify-center">
            <p className="text-caption text-base-content/70">Loading conversation…</p>
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
          <TurnError
            error={error}
            canRetry={canRetry}
            retryAfter={retryAfter}
            onRetry={() => void retry()}
          />
        )}
        {conversation?.archived_at ? (
          // Read-only until unarchived (S61): the server refuses a message here with 409.
          <div className="border-base-300 mx-auto flex w-full max-w-3xl items-center gap-3 border-t px-6 py-4">
            <p className="text-body text-base-content/70">This conversation is archived.</p>
            <button
              type="button"
              className="btn btn-sm"
              disabled={unarchive.isPending}
              onClick={() => unarchive.mutate({ id: conversation.id, archived: false })}
            >
              Unarchive to continue
            </button>
          </div>
        ) : (
          <>
            {practicePaused ? (
              <PracticePausedStrip
                onResume={handleResume}
                onSkip={handleSkip}
                disabled={practiceBusy}
              />
            ) : (
              awaitingReply && <PracticeControls onSkip={handleSkip} disabled={practiceBusy} />
            )}
            <Composer
              disabled={!!pending}
              streaming={!!pending}
              onStop={stop}
              onSend={(content, mode) => send(content, { mode })}
            />
          </>
        )}
      </main>
      <SidePanel
        open={citation !== null}
        onClose={() => setCitation(null)}
        side="right"
        label="Source"
        width="w-80"
        showClose={false}
      >
        {citation && <CitationPane citation={citation} onClose={() => setCitation(null)} />}
      </SidePanel>
    </div>
  );
}
