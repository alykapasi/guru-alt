import { useEffect, useRef } from "react";
import { useParams } from "react-router-dom";
import { useChatConversation } from "../hooks/useChatConversation";
import { MessageList } from "../components/chat/MessageList";
import { Composer } from "../components/chat/Composer";
import { ItemPanel } from "../components/lessons/ItemPanel";

/** A guided-practice session: the workflow-mode chat transcript plus a persistent side panel
 * for the item being practiced (see docs/ROADMAP.md Phase 7's design brief). Reuses the same
 * conversation/SSE machinery as the plain chat page — a session is just a conversation whose
 * turns are always sent with mode "workflow". */
export function Session() {
  const { conversationId } = useParams<{ conversationId: string }>();
  const { messages, isLoadingMessages, pending, error, item, sessionDetail, send } =
    useChatConversation(conversationId);
  const hasStartedRef = useRef(false);

  useEffect(() => {
    if (isLoadingMessages || pending || hasStartedRef.current) return;
    if (messages.length === 0) {
      hasStartedRef.current = true;
      send("Let's begin.", { mode: "workflow" });
    }
  }, [isLoadingMessages, messages.length, pending, send]);

  // "practice" is mid-session (the learner should keep answering); "mastered"/"capped" are the
  // two ways a workflow run ends (see app/services/workflow.py::run_workflow_turn).
  const ended = sessionDetail === "mastered" || sessionDetail === "capped";

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
            onCitationClick={() => {}}
          />
        )}
        {error && (
          <p className="text-caption text-error mx-auto w-full max-w-3xl px-6 pb-2">{error}</p>
        )}
        <Composer
          disabled={!!pending || ended}
          onSend={(content) => send(content, { mode: "workflow" })}
          fixedMode="workflow"
          placeholder="Your answer…"
        />
      </div>
      <ItemPanel item={item} detail={sessionDetail} />
    </div>
  );
}
