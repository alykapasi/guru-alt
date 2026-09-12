import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useChatConversation } from "../hooks/useChatConversation";
import { MessageList } from "../components/chat/MessageList";
import { Composer } from "../components/chat/Composer";
import { ItemPanel } from "../components/lessons/ItemPanel";
import { CitationPane } from "../components/chat/CitationPane";
import type { Citation } from "../api/sse";

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
    send,
  } = useChatConversation(conversationId);
  const hasStartedRef = useRef(false);
  const [citation, setCitation] = useState<Citation | null>(null);

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
        <Composer
          disabled={!!pending || ended}
          onSend={(content) => send(content, { mode: "workflow" })}
          fixedMode="workflow"
          placeholder="Your answer…"
        />
      </div>
      {/* Evidence stacks above the question rather than replacing it: a learner opening a
          citation is checking a source *in order to answer*, so hiding the item they are
          answering to show it would defeat the click. */}
      <aside className="border-base-300 divide-base-300 flex w-80 shrink-0 flex-col divide-y border-l">
        {citation && <CitationPane citation={citation} onClose={() => setCitation(null)} />}
        <ItemPanel item={item} detail={sessionDetail} />
      </aside>
    </div>
  );
}
