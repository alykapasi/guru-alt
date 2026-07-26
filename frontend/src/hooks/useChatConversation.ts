import { useCallback, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useConversations, useMessages } from "../api/hooks";
import { streamTurn, type ItemEvent, type SendMessageBody } from "../api/sse";

export interface PendingTurn {
  userContent: string;
  assistantText: string;
  toolCalls: string[];
}

/** Owns one conversation's persisted history plus the live in-flight turn. Persisted messages
 * come from TanStack Query; the turn currently streaming is local state, refetched into
 * persisted history once the backend has actually written it (every terminal SSE event —
 * done/awaiting_reply/committed — means the backend already persisted its side).
 *
 * Local turn state (pending/error) is per-conversation and must not carry over on switch —
 * the caller keys its consuming component on conversationId (see App.tsx's ChatRoute) so
 * switching conversations remounts this hook fresh, rather than resetting state in an effect. */
export function useChatConversation(conversationId: string | undefined) {
  const queryClient = useQueryClient();
  const conversationsQuery = useConversations();
  const messagesQuery = useMessages(conversationId);
  const [pending, setPending] = useState<PendingTurn | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Every mode's `awaiting_reply`/`done` SSE event carries the practice item currently in play
  // (grounding-only in plain chat, the thing being graded in workflow mode) — tracked generically
  // here so a guided-practice session page can read it without a mode-specific hook.
  const [item, setItem] = useState<ItemEvent | null>(null);
  const [sessionDetail, setSessionDetail] = useState<string | null>(null);
  const [awaitingReply, setAwaitingReply] = useState(false);

  const conversation = conversationsQuery.data?.find((c) => c.id === conversationId);
  const messages = messagesQuery.data ?? [];
  const lastMessage = messages[messages.length - 1];
  // Every assistant reply while a conversation has no committed goal yet is a refinement-gate
  // proposal awaiting accept/refine (app/services/refinement.py) — reconstructed from persisted
  // state so this is correct after a reload, not just live.
  const awaitingGoalAccept =
    !pending &&
    conversation != null &&
    conversation.goal == null &&
    lastMessage?.role === "assistant";

  const send = useCallback(
    async (
      content: string,
      opts: { mode: "chat" | "agentic" | "workflow"; satisfied?: boolean },
    ) => {
      if (!conversationId || pending) return;
      setError(null);
      setPending({ userContent: content, assistantText: "", toolCalls: [] });

      const body: SendMessageBody = {
        content,
        mode: opts.mode,
        satisfied: opts.satisfied ?? false,
      };
      let goalCommitted = false;
      try {
        for await (const ev of streamTurn(conversationId, body)) {
          if (ev.type === "token") {
            setPending((p) => (p ? { ...p, assistantText: p.assistantText + ev.text } : p));
          } else if (ev.type === "tool_call") {
            setPending((p) => (p ? { ...p, toolCalls: [...p.toolCalls, ev.detail] } : p));
          } else if (ev.type === "error") {
            setError(ev.detail);
          } else if (ev.type === "committed") {
            goalCommitted = true;
          } else if (ev.type === "awaiting_reply") {
            setItem(ev.item);
            setSessionDetail(ev.detail);
            setAwaitingReply(true);
          } else if (ev.type === "done") {
            setItem(ev.item);
            setSessionDetail(ev.detail);
            setAwaitingReply(false);
          }
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "Something went wrong reaching the tutor.");
      }

      await queryClient.invalidateQueries({ queryKey: ["messages", conversationId] });
      if (goalCommitted) {
        await queryClient.invalidateQueries({ queryKey: ["conversations"] });
      }
      setPending(null);
    },
    [conversationId, pending, queryClient],
  );

  return {
    conversation,
    messages,
    isLoadingMessages: messagesQuery.isLoading,
    pending,
    error,
    awaitingGoalAccept,
    item,
    sessionDetail,
    awaitingReply,
    send,
  };
}
