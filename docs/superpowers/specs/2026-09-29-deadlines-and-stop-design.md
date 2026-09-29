# Whole-request deadlines and stopping a reply (S47, remainder)

**Status:** approved in conversation 2026-09-29; this document records it.
**Tracker:** S47 (its last remaining part). Workstream 5, piece 1 (before S49, S37, S17, S62,
S53). Follows the [spend limits design](2026-09-28-spend-limits-and-accounting-design.md), which
left "deadlines and cancellation" as the next slice.

## Problem

- `llm_timeout_seconds` (60) bounds each provider *read*, not a call, and each call may retry
  twice. A turn makes several calls (intent gate, grading, an agentic tool loop), and nothing
  bounds the turn. Non-streamed endpoints (lesson, curriculum, notes, onboarding generation) are
  unbounded too.
- A learner cannot stop a reply. The browser aborts only on unmount; a disconnect cancels the
  request, the meter settles the call `partial`, and the turn is reaped as `cancelled` on the
  next read — the text the learner saw is lost, and "stopped" is indistinguishable from "the
  network dropped".

## Decisions (from the design conversation)

1. **Stop keeps the partial reply (A):** saved as the assistant's reply, marked stopped; the
   conversation continues from it.
2. **Stop is an explicit request (1):** the browser asks the server to stop and keeps reading;
   the server ends the stream itself. A disconnect stays `cancelled`. The signal travels by
   Postgres `LISTEN/NOTIFY` on the connection the turn's lock already holds (refined from Redis
   during design: same behaviour, cross-process, nothing new in the web app).
3. **Deadlines:** a streamed turn ends like a Stop with its text kept; a non-streamed request
   answers 504.

## Behaviour

### Stop

- `POST /api/v1/conversations/{conversation_id}/turns/{turn_id}/stop` → **202**. Only the
  conversation's owner (404 otherwise, as every conversation route). **409** when the turn is
  not `pending`, or is `pending` but not running (`turn_lock.is_active` false).
- The running turn stops consuming its flow at the next await. Closing the flow closes the
  provider stream; the meter already settles that call `partial`.
- Text streamed so far is saved as an assistant message with `interrupted="stopped"`. No text
  yet: no message. The turn closes `STOPPED` (new `TurnStatus`). The stream ends with a terminal
  SSE event `{"type": "stopped", "message_id": ...}`.
- Stopping is as safe as a disconnect today: uncommitted work rolls back, a committed grade
  stands, guided practice resumes from its last checkpoint.

### Deadlines

- **Streamed turns** (chat; onboarding's stream too, without Stop) run for at most
  `turn_deadline_seconds` (default 120). On expiry: text so far saved with
  `interrupted="timed_out"`, call settled `partial`, turn closed `FAILED` with error
  `deadline`, and the stream ends with `{"type": "error", "detail": "This reply took too long and
  was cut off.", "code": "deadline"}`.
- **Everything else** runs for at most `request_deadline_seconds` (default 180) until its
  response starts; on expiry the handler is cancelled and the answer is **504**
  `{"detail": "deadline_exceeded"}`. An unfinished call is settled `partial` by the meter.
- Both settings are uncalibrated and are added to S18's constants inventory.

### Data

- `TurnStatus.STOPPED = "stopped"`.
- `messages.interrupted: Text NULL` — `stopped` | `timed_out`. Migration `0074_turn_stop`; existing
  rows NULL.
- A saved partial reply is part of the transcript: later turns' history includes it, as the
  learner saw it. `MessageRead` exposes `interrupted`; `TurnRead.status` may be `stopped`.

## Design

### `app/services/turn_control.py`

- `TurnControl` wraps a flow's async event stream. Each `__anext__` of the flow is raced against
  a stop `asyncio.Event` and the remaining deadline. Stop or expiry → `aclose()` the flow, then
  yield exactly one terminal marker (`stopped` or `timed_out`) and end. Otherwise events pass
  through unchanged.
- **Listening:** at the start of a turn, the claim's connection (`TurnClaim.connection`, held in
  AUTOCOMMIT for the turn) runs `LISTEN turn_stop` through asyncpg's `add_listener`; a payload
  equal to this turn's id sets the stop event. The listener is removed when the turn ends. With
  the fallback claim (no connection), stops go through an in-process registry keyed by turn id —
  the same single-process degradation the lock already has. A failure to listen logs a warning
  and the turn runs without Stop; the deadline still holds.
- `request_stop(session, turn_id)` sends `SELECT pg_notify('turn_stop', :turn_id)` (and sets the
  in-process registry entry). The payload is an id only — never learner text.

### `app/api/v1/chat.py`

- `event_stream` iterates `TurnControl(...)` instead of the raw stream, accumulates token text,
  and on the terminal marker writes the partial message (`add_message(..., interrupted=...)`),
  yields the terminal event, records the phase as for any turn with no answer awaited, and closes
  the turn (`STOPPED`, or `FAILED` + `deadline`).
- The new stop route lives beside `list_turns`.

### `RequestDeadlineMiddleware` (`app/core/deadline.py`)

A pure ASGI middleware: runs the app under `request_deadline_seconds` until
`http.response.start` is sent, then lifts the limit — so SSE responses, which start at once, are
governed by `TurnControl`. On expiry before start, it cancels the app and sends 504. Covers every
route with no per-endpoint wrapping.

### Frontend

- `useChatConversation` gains `stop()`: POST the stop route, keep reading until the terminal
  event. While streaming, Send becomes **Stop**.
- A message with `interrupted="stopped"` shows "Stopped"; `timed_out` shows "This reply took too
  long and was cut off" with the existing Retry (same `client_turn_id`, so it regenerates).
- API types regenerated (`uv run poe api-types`), contract checked (`uv run poe api-contract`).

## Errors

- Stop racing completion: the turn finished first → 409; the client ignores it (`done` arrived).
- Stop on a dead or stale turn → 409. Another learner's conversation → 404.
- Listening fails → turn runs without Stop, warning logged; the deadline still applies.
- Deadline during grading: the uncommitted grade rolls back; a retry grades once (attempt id
  derives from `client_turn_id`, S34).

## Testing

- `TurnControl`: stop mid-stream closes the flow and yields one `stopped`; expiry yields one
  `timed_out`; neither passes events through unchanged; stop before any token.
- Meter: a stopped provider stream settles `partial`.
- API: stop → 202, stream ends `stopped`, partial message saved with `interrupted="stopped"`, turn
  `stopped`; stop on a completed turn → 409; on another learner's → 404; a timed-out turn is
  `failed`/`deadline` with its partial message, and the next turn's history includes it.
- Cross-connection: a `NOTIFY` from a second connection stops a turn whose claim connection is
  listening.
- Middleware: a slow JSON route answers 504 and its handler is cancelled; an SSE route runs past
  the limit.
- Migration 0074 (existing messages NULL); `db-check` clean.
- Vitest: Stop replaces Send while streaming; the stopped and timed-out notes render.

## Docs

Tracker S47: Implemented (deadlines and Stop). S18: the two new constants. CLAUDE.md spend
bullet: a turn has a deadline and can be stopped, keeping its text. RUNBOOK §18: the two settings.

## Out of scope

Stop for guided-practice controls and onboarding; background-job deadlines (S37/S17);
calibrating the limits; provider rate-limit and outage handling (S49).
