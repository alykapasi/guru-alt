import { useState } from "react";
import { Navigate, useParams } from "react-router-dom";
import { useChatConversation } from "../hooks/useChatConversation";
import { MessageList } from "../components/chat/MessageList";
import { Composer } from "../components/chat/Composer";
import { CitationPane } from "../components/chat/CitationPane";
import type { Citation } from "../api/sse";

export function Chat() {
  const { conversationId } = useParams<{ conversationId: string }>();
  const { conversation, messages, isLoadingMessages, pending, error, awaitingGoalAccept, send } =
    useChatConversation(conversationId);
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
        {error && (
          <p className="text-caption text-error mx-auto w-full max-w-3xl px-6 pb-2">{error}</p>
        )}
        <Composer disabled={!!pending} onSend={(content, mode) => send(content, { mode })} />
      </div>
      {citation && <CitationPane citation={citation} onClose={() => setCitation(null)} />}
    </div>
  );
}
