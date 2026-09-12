import { useState } from "react";
import { Navigate, useParams } from "react-router-dom";
import { useChatConversation } from "../hooks/useChatConversation";
import { MessageList } from "../components/chat/MessageList";
import { CheckResultCard } from "../components/chat/CheckResultCard";
import { Composer } from "../components/chat/Composer";
import { CitationPane } from "../components/chat/CitationPane";
import type { Citation } from "../api/sse";

export function Chat() {
  const { conversationId } = useParams<{ conversationId: string }>();
  const {
    conversation,
    messages,
    isLoadingMessages,
    pending,
    error,
    canRetry,
    retry,
    awaitingGoalAccept,
    checkResult,
    send,
  } = useChatConversation(conversationId);
  const [citation, setCitation] = useState<Citation | null>(null);

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
          />
        )}
        {checkResult && (
          <div className="px-6 pb-3">
            <CheckResultCard result={checkResult} />
          </div>
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
