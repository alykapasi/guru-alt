import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useConversations, useItem, useMessages } from "../api/hooks";
import { isTerminal, streamTurn, type ItemEvent, type SendMessageBody } from "../api/sse";

export interface PendingTurn {
  userContent: string;
  assistantText: string;
  toolCalls: string[];
}

/** A turn that ended without the backend recording a reply, kept so it can be tried again
 * with the same identity — the retry regenerates rather than re-asking (S51). */
interface FailedTurn {
  content: string;
  clientTurnId: string;
  opts: { mode: "chat" | "agentic" | "workflow"; satisfied?: boolean };
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
  const [failed, setFailed] = useState<FailedTurn | null>(null);
  // Every mode's `awaiting_reply`/`done` SSE event carries the practice item currently in play
  // (grounding-only in plain chat, the thing being graded in workflow mode) — tracked generically
  // here so a guided-practice session page can read it without a mode-specific hook.
  const [liveItem, setLiveItem] = useState<ItemEvent | null>(null);
  const [sessionDetail, setSessionDetail] = useState<string | null>(null);
  const [liveAwaitingReply, setLiveAwaitingReply] = useState(false);

  // Abort the in-flight turn when this hook goes away. Without it the fetch outlived the
  // component that started it: tokens kept arriving for a conversation nobody was looking at,
  // and its state setters ran against an unmounted tree. The backend sees the disconnect and
  // records the turn as cancelled rather than leaving it pending forever.
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

  const conversation = conversationsQuery.data?.find((c) => c.id === conversationId);
  const messages = messagesQuery.data ?? [];
  // The backend records what the conversation is waiting for at the end of every turn
  // (app/api/v1/chat.py::_phase_after), so both of these survive a reload and neither has to
  // be guessed from the transcript's shape. The previous guess — "no goal committed and the
  // last message is from the assistant" — was equally true of an agentic reply given before
  // any goal existed, so a tool-using answer was offered with accept/refine buttons.
  // While a turn is streaming the SSE events are ahead of the server read, so they win; once
  // it ends the persisted phase is authoritative.
  const awaitingGoalAccept = !pending && conversation?.phase === "goal_proposed";
  const awaitingReply = pending ? liveAwaitingReply : conversation?.phase === "awaiting_answer";

  // On reload there is no live item, only the id the phase points at — fetch it so a paused
  // session comes back showing the question it was actually on rather than an empty panel.
  const restoredItem = useItem(pending || liveItem ? null : conversation?.active_item_id);
  const item = liveItem ?? (restoredItem.data as ItemEvent | undefined) ?? null;

  const run = useCallback(
    async (turn: FailedTurn) => {
      if (!conversationId) return;
      setError(null);
      setFailed(null);
      setPending({ userContent: turn.content, assistantText: "", toolCalls: [] });

      const body: SendMessageBody = {
        content: turn.content,
        mode: turn.opts.mode,
        satisfied: turn.opts.satisfied ?? false,
        client_turn_id: turn.clientTurnId,
      };
      const controller = new AbortController();
      abortRef.current = controller;
      // A stream can stop for reasons that are not an ending: the connection drops, the server
      // restarts, a proxy times out. Until this was tracked, the generator simply running out
      // was read as success, so a half-written explanation was left on screen looking finished.
      let sawTerminal = false;
      try {
        for await (const ev of streamTurn(conversationId, body, controller.signal)) {
          if (isTerminal(ev)) sawTerminal = true;
          if (ev.type === "token") {
            setPending((p) => (p ? { ...p, assistantText: p.assistantText + ev.text } : p));
          } else if (ev.type === "tool_call") {
            setPending((p) => (p ? { ...p, toolCalls: [...p.toolCalls, ev.detail] } : p));
          } else if (ev.type === "error") {
            setError(ev.detail);
            setFailed(turn);
          } else if (ev.type === "awaiting_reply") {
            setLiveItem(ev.item);
            setSessionDetail(ev.detail);
            setLiveAwaitingReply(true);
          } else if (ev.type === "done") {
            setLiveItem(ev.item);
            setSessionDetail(ev.detail);
            setLiveAwaitingReply(false);
          }
        }
        if (!sawTerminal) {
          setError("The reply was cut off before it finished. You can try again.");
          setFailed(turn);
        }
      } catch (e) {
        // An abort is us ending the turn, not the turn failing — say nothing about it.
        if (!controller.signal.aborted) {
          setError(e instanceof Error ? e.message : "Something went wrong reaching the tutor.");
          setFailed(turn);
        }
      } finally {
        abortRef.current = null;
      }

      await queryClient.invalidateQueries({ queryKey: ["messages", conversationId] });
      // Always, not only on commit: the turn just recorded the conversation's phase, and a
      // stale cached phase is exactly the bug this replaced.
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
      await queryClient.invalidateQueries({ queryKey: ["turns", conversationId] });
      setPending(null);
    },
    [conversationId, queryClient],
  );

  const send = useCallback(
    async (
      content: string,
      opts: { mode: "chat" | "agentic" | "workflow"; satisfied?: boolean },
    ) => {
      if (!conversationId || pending) return;
      await run({ content, clientTurnId: crypto.randomUUID(), opts });
    },
    [conversationId, pending, run],
  );

  /** Try the last unfinished turn again, under its original identity. */
  const retry = useCallback(async () => {
    if (!failed || pending) return;
    await run(failed);
  }, [failed, pending, run]);

  return {
    conversation,
    messages,
    isLoadingMessages: messagesQuery.isLoading,
    pending,
    error,
    canRetry: failed !== null && !pending,
    retry,
    awaitingGoalAccept,
    item,
    sessionDetail,
    awaitingReply,
    send,
  };
}
