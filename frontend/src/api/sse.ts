import { apiFetch } from "./client";

export interface UsageEvent {
  input_tokens: number;
  output_tokens: number;
}

export interface ItemEvent {
  id: string;
  item_type: string;
  stem: string;
  difficulty: number;
  rubric_id: string | null;
  kcs: { kc_id: string; weight: number }[];
}

/** What happened to an answer the learner gave in conversation (S15) — mirrors
 * app/schemas/chat.py's CheckResultRead.
 *
 * Every optional field is optional because the evidence may genuinely not exist, not because
 * it was inconvenient to fill in: `score` is null where the grader could not tell the
 * components apart (an MCQ has one outcome, S10), and `failure_kind` is null where nothing
 * diagnosed it (S09). Rendering has to keep that difference — "we could not tell" and "it was
 * fine" are not the same thing to show somebody. */
export interface CheckComponent {
  kc_id: string;
  kc_name: string;
  score: number | null;
  /** Logit-scale ability before and after this answer — both, so the movement can be shown
   * as a movement rather than a number with no baseline. See lib/mastery.ts. */
  prior_ability: number;
  ability: number;
  uncertainty: number;
  failure_kind: string | null;
  /** The grader's own sentence. Confidence is deliberately not carried: a model's
   * self-reported confidence is not calibrated, and a number implies it is. */
  failure_detail: string | null;
  /** How many *earlier* attempts on this component failed the same way (S09). Null when
   * nothing was diagnosed, 0 when this is the first time. A count of independent observations
   * is the one signal here that does not rest on the model being calibrated. */
  recurrence: number | null;
}

export interface CheckResult {
  item_id: string;
  score: number;
  correct: boolean;
  components: CheckComponent[];
}

/** The literal [N] marker in a message's content, mapped to the chunk it cites — see
 * app/services/turn_common.py's format_grounding/extract_citations. */
export interface Citation {
  marker: number;
  chunk_id: string;
  source_id: string;
}

/** Mirrors app/api/v1/chat.py's event_stream() frame shapes exactly. Not derivable from the
 * OpenAPI schema — FastAPI's StreamingResponse body isn't typed there — so this union is
 * hand-maintained alongside the backend's `_sse()` calls. */
export type TurnEvent =
  | { type: "token"; text: string }
  | { type: "error"; detail: string }
  | {
      type: "done";
      message_id: string | null;
      usage: UsageEvent;
      cost_usd: number | null; // null = the model has no known price
      item: ItemEvent | null;
      detail: string;
      citations: Citation[];
      /** Present only on a turn that graded an answer the learner gave in conversation. */
      check_result: CheckResult | null;
    }
  | {
      type: "awaiting_reply";
      text: string;
      detail: string;
      item: ItemEvent | null;
      citations: Citation[];
      /** Guided practice reports the attempt it just graded here, mid-round — the learner is
       * about to answer the same question again, which is when it matters most. */
      check_result: CheckResult | null;
    }
  | { type: "committed"; goal: string; detail: string }
  | { type: "tool_call"; detail: string };

export interface SendMessageBody {
  content: string;
  satisfied?: boolean;
  mode?: "chat" | "agentic" | "workflow";
  /** This turn's identity, chosen by us (S51). Re-sending the same key retries *that* turn:
   * the backend regenerates from the learner message it already stored instead of appending
   * the question again, and refuses outright if the turn already produced a reply. */
  client_turn_id?: string;
}

/** The SSE frames that mean the turn reached an end the backend recorded. A stream that stops
 * without one of these did not finish — it was cut off — which is precisely what the old
 * "the generator ended, so we're done" reading could not tell apart. */
export const TERMINAL_EVENTS = ["done", "awaiting_reply", "committed", "error"] as const;

export function isTerminal(event: TurnEvent): boolean {
  return (TERMINAL_EVENTS as readonly string[]).includes(event.type);
}

/**
 * Streams one conversation turn. `POST /conversations/{id}/messages` returns an SSE body over
 * a POST, so the native `EventSource` API (GET-only) doesn't apply — read the response body's
 * `ReadableStream` directly and parse `data: {...}\n\n` frames by hand.
 */
export async function* streamTurn(
  conversationId: string,
  body: SendMessageBody,
  signal?: AbortSignal,
): AsyncGenerator<TurnEvent> {
  const res = await apiFetch(`/api/v1/conversations/${conversationId}/messages`, {
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
      yield JSON.parse(line.slice("data: ".length)) as TurnEvent;
    }
  }
}
