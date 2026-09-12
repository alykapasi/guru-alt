import { useEffect, useRef } from "react";
import { MessageBlock } from "./MessageBlock";
import { ToolCallChip } from "./ToolCallChip";
import { CheckResultCard } from "./CheckResultCard";
import type { components } from "../../api/schema";
import type { CheckResult, Citation } from "../../api/sse";
import type { PendingTurn } from "../../hooks/useChatConversation";

type Message = components["schemas"]["MessageRead"];

export function MessageList({
  messages,
  pending,
  goal,
  awaitingGoalAccept,
  onAcceptGoal,
  onCitationClick,
  hasEarlier = false,
  isLoadingEarlier = false,
  onLoadEarlier,
}: {
  messages: Message[];
  pending: PendingTurn | null;
  goal: string | null | undefined;
  awaitingGoalAccept: boolean;
  onAcceptGoal: () => void;
  onCitationClick: (citation: Citation) => void;
  hasEarlier?: boolean;
  isLoadingEarlier?: boolean;
  onLoadEarlier?: () => void;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);

  // Keyed on the *newest* message, not on how many there are (S62). Loading earlier messages
  // grows the list from the top, and a length-keyed effect read that as new activity and threw
  // the reader back to the bottom — away from the thing they had just asked to see.
  const newestId = messages.length ? messages[messages.length - 1].id : null;
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [newestId, pending?.assistantText, pending?.toolCalls.length]);

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-5 overflow-y-auto px-6 py-8">
      {goal && (
        <p className="text-caption text-base-content/50 border-base-300 -mt-2 border-b pb-4">
          Goal: {goal}
        </p>
      )}
      {hasEarlier && (
        <button
          type="button"
          onClick={onLoadEarlier}
          disabled={isLoadingEarlier}
          className="text-caption text-base-content/60 hover:bg-base-200 hover:text-base-content rounded-field mx-auto px-3 py-2 transition-colors disabled:opacity-50"
        >
          {isLoadingEarlier ? "Loading…" : "Load earlier messages"}
        </button>
      )}
      {messages.map((m) => (
        <div key={m.id} className="flex flex-col gap-3">
          <MessageBlock
            role={m.role}
            content={m.content}
            citations={m.citations as unknown as Citation[]}
            onCitationClick={onCitationClick}
          />
          {/* Rendered from the transcript rather than from the stream, so it survives a
              reload and scrolls back with the conversation it belongs to. The turn that
              reported it is the turn it sits under. */}
          {m.check_result && <CheckResultCard result={m.check_result as unknown as CheckResult} />}
        </div>
      ))}
      {pending && (
        <>
          <MessageBlock role="user" content={pending.userContent} />
          {pending.toolCalls.length > 0 && (
            <div className="flex flex-wrap gap-2 pl-11">
              {pending.toolCalls.map((tool, i) => (
                <ToolCallChip key={i} tool={tool} />
              ))}
            </div>
          )}
          <MessageBlock role="assistant" content={pending.assistantText} streaming />
        </>
      )}
      {awaitingGoalAccept && (
        <button onClick={onAcceptGoal} className="btn btn-outline btn-primary btn-sm ml-11 w-fit">
          Sounds good, let's start
        </button>
      )}
      <div ref={bottomRef} />
    </div>
  );
}
