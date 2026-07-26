import { useEffect, useRef } from "react";
import { MessageBlock } from "./MessageBlock";
import { ToolCallChip } from "./ToolCallChip";
import type { components } from "../../api/schema";
import type { Citation } from "../../api/sse";
import type { PendingTurn } from "../../hooks/useChatConversation";

type Message = components["schemas"]["MessageRead"];

export function MessageList({
  messages,
  pending,
  goal,
  awaitingGoalAccept,
  onAcceptGoal,
  onCitationClick,
}: {
  messages: Message[];
  pending: PendingTurn | null;
  goal: string | null | undefined;
  awaitingGoalAccept: boolean;
  onAcceptGoal: () => void;
  onCitationClick: (citation: Citation) => void;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages.length, pending?.assistantText, pending?.toolCalls.length]);

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-5 overflow-y-auto px-6 py-8">
      {goal && (
        <p className="text-caption text-base-content/50 border-base-300 -mt-2 border-b pb-4">
          Goal: {goal}
        </p>
      )}
      {messages.map((m) => (
        <MessageBlock
          key={m.id}
          role={m.role}
          content={m.content}
          citations={m.citations as unknown as Citation[]}
          onCitationClick={onCitationClick}
        />
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
