# Provider rate limits and outages, told plainly (S49, remainder)

**Status:** approved in conversation 2026-09-30; this document records it.
**Tracker:** S49 (its last remaining part). Workstream 5, piece 2 (after S47; before S37, S17,
S62, S53). Builds on S49's contract work (registry validation, `GURU_LLM_TIMEOUT_SECONDS`,
`GURU_LLM_MAX_RETRIES`), which this does not revisit.

## Problem

The SDKs retry a 429 or 5xx twice (`llm_max_retries`). After that the SDK's exception escapes
as-is:

- a streamed chat turn flattens it to `{"type": "error", "detail": "generation failed"}` —
  indistinguishable from a bug in Guru;
- every other route answers a bare 500;
- background work logs it as a failure (memory write-back re-raises; the sweep retries only
  after `refresh_retry_minutes`), and a source being ingested fails with a generic error;
- the meter settles the call `failed` with nothing saying the provider refused it.

## Decisions (from the design conversation)

1. **Classify and tell (A).** No retries of our own beyond the SDK's, no circuit breaker, no
   fallback provider. Recognise the failure and give every surface one predictable answer.
2. **Chat: say so, manual Retry.** The existing Retry (same `client_turn_id`) stays the only
   way to try again; after a *busy* failure it is disabled until the suggested wait has passed.
3. **Partial text is discarded.** A reply the provider broke off is not saved (unlike an S47
   deadline); Retry regenerates it.
4. **One refusal type.** `CallRefused` becomes the base of `BudgetExceeded` and the new
   `ProviderUnavailable`; the places that already turn a spend refusal into a reason — route
   handler, `refusal_ends_turn`, worker deferral — catch the base.

## Behaviour

### Classification (`app/llm/providers/*` — the only SDK importers)

After the SDK's own retries are exhausted:

| Provider answer | Raised | `retry_after` |
| --- | --- | --- |
| 429 | `ProviderUnavailable(kind="busy")` | the `retry-after` header in seconds, else `provider_retry_after_seconds` (20) |
| 5xx, Anthropic 529 overloaded, connection error, SDK timeout | `ProviderUnavailable(kind="down")` | `None` |
| anything else (400, 401, 403, 404, …) | unchanged SDK exception | — |

The same mapping applies to a stream that fails before its first chunk and one that fails
part-way. `LLMClient`'s meter settles the call `failed` with `error` = `provider_busy` or
`provider_down` (today: the exception text).

Messages (on the exception, like `BudgetExceeded.message`):

- busy: "The tutor is busy right now — try again in a few seconds."
- down: "The tutor can't be reached right now — try again shortly."

### Surfaces

- **Non-streamed routes:** an app exception handler answers **503**
  `{"detail": {"code": "provider_busy"|"provider_down", "message": …, "retry_after": 20|null}}`,
  with a `Retry-After` header when `retry_after` is set. `BudgetExceeded` keeps its 429 and its
  shape.
- **Streamed turns (chat, onboarding):** `refusal_ends_turn` catches `CallRefused`; a provider
  refusal yields `{"type": "error", "detail": message, "code": "provider_busy"|"provider_down",
  "retry_after": …}`. Flows' `except BudgetExceeded: raise` become `except CallRefused: raise`,
  so generation's own `except Exception` no longer swallows it. The turn closes `FAILED` with
  error `provider_busy`/`provider_down`; no assistant message is saved; a retry with the same
  `client_turn_id` regenerates (already true of a failed turn).
- **Checks graded inside a turn:** unchanged in effect — the check stays open and the turn goes
  on; the `except BudgetExceeded` there becomes `except CallRefused`.
- **Background work:** every task that defers on `BudgetExceeded` (memory write-back, profile
  refresh, concept links) defers on `CallRefused`, logging `provider.deferred task=…` for a
  provider refusal; the claim stays and the sweep retries.
- **Ingestion:** a provider refusal is treated like a spend refusal — the source fails with the
  refusal's message as its error (not a generic one), and re-processing stays the learner's
  confirmed decision (S50).
- **Other `except BudgetExceeded` sites** are each reviewed in the plan and switched to
  `CallRefused` only where "stop with the reason / defer" is the right reading.

### Frontend

- `failureMessage` already reads `detail.message`; non-streamed screens need no change.
- Chat: the SSE error event type gains `retry_after?: number | null`. After an error with
  `code: "provider_busy"`, Retry is disabled and labelled with a countdown until `retry_after`
  seconds have passed; after `provider_down` (or any other error) Retry is enabled at once.

## Design notes

- `CallRefused(Exception)` lives in `app/llm/meter.py` beside `BudgetExceeded`, carrying
  `message`; `ProviderUnavailable(CallRefused)` adds `kind` and `retry_after`. Exported from
  `app/llm` so services never import a provider.
- Setting `provider_retry_after_seconds: float = 20.0` (`GURU_PROVIDER_RETRY_AFTER_SECONDS`),
  uncalibrated, added to S18's constants inventory.
- `FakeProvider` gains a way to raise `ProviderUnavailable` (before or during a stream) so tests
  exercise the real paths without an SDK.

## Errors

- A 401/403 (bad key) stays a 500 and a loud log — it is configuration, and telling a learner
  "busy" would hide it.
- A malformed `retry-after` header falls back to the default; an HTTP-date form is parsed.
- A provider refusal inside an S47 deadline is whichever happens first; a deadline still keeps
  its text, a refusal does not.

## Testing

- Adapters (Anthropic, OpenAI-compatible): SDK errors built from fake responses map as the
  table says, for `complete`, a stream failing before its first chunk, and one failing
  part-way; 400/401 pass through untouched.
- Meter: a refused call settles `failed` with `error="provider_busy"`.
- API: a non-streamed route over a busy provider → 503, shape, `Retry-After`; `BudgetExceeded`
  still → 429 `budget_exceeded`.
- Chat: a busy provider ends the stream with the coded error event, the turn `failed` /
  `provider_busy`, no assistant message; the same `client_turn_id` then answers.
- Workers: write-back and profile refresh defer and keep their claims.
- Ingestion: a refused embed fails the source with the busy message.
- Vitest: Retry disabled with a countdown after `provider_busy`, enabled after it elapses;
  enabled at once after `provider_down`.

## Docs

Tracker S49 → Completed. RUNBOOK §18: a "Provider failures" bullet (codes, 503 and
`Retry-After`, deferral, `error` values on spend rows). CLAUDE.md spend bullet: one clause.
S18: `provider_retry_after_seconds`.

## Out of scope

Fallback providers or models, a circuit breaker, retries beyond the SDK's, calibrating the
default wait.
