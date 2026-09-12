import { useCallback, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "./client";

// ============================================================================
// Types
// ============================================================================

export type OnboardingTurnEvent =
  | { type: "token"; text: string }
  | { type: "awaiting_reply"; text: string; detail: string }
  | { type: "committed"; goal: string; detail: string }
  | { type: "error"; detail: string };

export interface KCProposal {
  name: string;
  description: string;
  /** Stable handle for this KC within one proposal, assigned by the backend's parser. */
  key?: string;
  /** Keys of the KCs that come first — the prerequisite edges the planner orders by (S22).
   * Declared so the review step's edits carry them: they survive today only because every
   * handler spreads the object, and a refactor that rebuilt one would drop them silently.
   * Renaming a KC is safe (edges reference keys, not names), and deleting one leaves a
   * dangling reference that the backend drops rather than failing the commit. */
  requires?: string[];
}

export interface TopicProposal {
  name: string;
  description: string;
  kcs: KCProposal[];
}

export interface CurriculumProposal {
  subject_name: string;
  subject_description: string;
  topics: TopicProposal[];
}

export interface GoalTurnBody {
  session_id: string;
  content: string;
  satisfied: boolean;
  mode: "start" | "resume";
}

export interface SubjectCommitPayload {
  subject_name: string;
  subject_description: string | null;
  topics: TopicProposal[];
  source_ids: string[] | null;
}

export interface CreatedSubject {
  id: string;
  slug: string;
  name: string;
  description: string | null;
}

// ============================================================================
// SSE Streaming
// ============================================================================

/**
 * Asks the server for a goal-refinement session id.
 *
 * The client used to invent this id, which meant the server keyed a negotiation's state on a
 * value it had never issued and could not attribute to anyone.
 */
export async function createGoalSession(): Promise<string> {
  const res = await apiFetch(`/api/v1/onboarding/goal-sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  if (!res.ok) throw new Error(`could not start a goal session: ${res.status}`);
  return ((await res.json()) as { session_id: string }).session_id;
}

/**
 * Streams one goal-refinement turn. Mirrors sse.ts's streamTurn pattern: POST returns an SSE
 * body, so we read the response body's ReadableStream directly and parse `data: {...}\n\n`
 * frames by hand.
 */
export async function* streamGoalTurn(
  body: GoalTurnBody,
  signal?: AbortSignal,
): AsyncGenerator<OnboardingTurnEvent> {
  const res = await apiFetch(`/api/v1/onboarding/goal-turns`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`stream failed: ${res.status} ${res.statusText}`);
  }

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const line = frame.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      yield JSON.parse(line.slice("data: ".length)) as OnboardingTurnEvent;
    }
  }
}

// ============================================================================
// Hooks
// ============================================================================

/**
 * The server-issued session id this wizard's negotiation runs under. Minted once and kept for
 * the life of the component; a new wizard is a new negotiation.
 */
export function useGoalSession() {
  return useQuery({
    queryKey: ["goal-session"],
    queryFn: createGoalSession,
    staleTime: Infinity,
    gcTime: Infinity,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
  });
}

/**
 * Owns the goal-refinement negotiation: local streaming state, the latest proposal text,
 * the committed goal (once accepted), and a send function that streams turns.
 *
 * Tracks whether this session has already started (to determine mode: "start" vs "resume").
 */
export function useGoalRefinement(sessionId: string | undefined) {
  const [proposal, setProposal] = useState<string>("");
  const [committedGoal, setCommittedGoal] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tokens, setTokens] = useState<string>("");
  const hasStartedRef = useRef(false);

  const send = useCallback(
    async (content: string, satisfied: boolean) => {
      if (!sessionId || isStreaming) return;
      setError(null);
      setTokens("");
      setIsStreaming(true);

      const mode: "start" | "resume" = hasStartedRef.current ? "resume" : "start";
      if (!hasStartedRef.current) {
        hasStartedRef.current = true;
      }

      try {
        for await (const ev of streamGoalTurn({
          session_id: sessionId,
          content,
          satisfied,
          mode,
        })) {
          if (ev.type === "token") {
            setTokens((t) => t + ev.text);
          } else if (ev.type === "awaiting_reply") {
            setProposal(ev.text);
            setTokens("");
          } else if (ev.type === "committed") {
            setCommittedGoal(ev.goal);
            setTokens("");
          } else if (ev.type === "error") {
            setError(ev.detail);
          }
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "Goal refinement failed.");
      } finally {
        setIsStreaming(false);
      }
    },
    [sessionId, isStreaming],
  );

  return { proposal, committedGoal, isStreaming, error, tokens, send };
}

/**
 * Mutation for generating a curriculum proposal from a committed goal and optional sources.
 */
export function useGenerateCurriculum() {
  return useMutation({
    mutationFn: async ({
      goal,
      sourceIds,
    }: {
      goal: string;
      sourceIds: string[] | null;
    }): Promise<CurriculumProposal> => {
      const res = await apiFetch(`/api/v1/onboarding/curriculum`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal, source_ids: sourceIds }),
      });
      if (!res.ok) {
        throw new Error(`curriculum generation failed: ${res.status} ${res.statusText}`);
      }
      return res.json();
    },
  });
}

/**
 * Mutation for committing a subject (with its knowledge graph structure) to the learner's
 * subjects. Invalidates the subjects query on success.
 */
export function useCommitSubject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: SubjectCommitPayload): Promise<CreatedSubject> => {
      const res = await apiFetch(`/api/v1/subjects/commit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const error = new Error(
          `subject commit failed: ${res.status} ${res.statusText}`,
        ) as Error & { status?: number };
        error.status = res.status;
        throw error;
      }
      return res.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["subjects"] });
    },
  });
}
