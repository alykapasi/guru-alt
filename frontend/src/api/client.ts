import createClient from "openapi-fetch";
import type { paths } from "./schema";

/** The base URL the typed client and the hand-rolled SSE helper both target. Dev default
 * matches app/core/config.py's cors_origins counterpart (the FastAPI dev server). */
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

/** Every request carries the session cookie (S21).
 *
 * `credentials: "include"` is not a detail — the API is a different origin from the app in
 * dev and may be in production, and `fetch` sends no cookies cross-origin unless told to. The
 * failure mode without it is that the browser holds a perfectly valid session and every
 * request is still a 401, which reads as a broken login rather than a missing option. */
export const CREDENTIALS: RequestCredentials = "include";

/** Fully-typed request client generated from the backend's OpenAPI schema (see
 * `npm run gen:api`). Every REST endpoint should go through this, not a raw fetch.
 *
 * `fetch` is passed as a wrapper rather than left to default, because openapi-fetch captures
 * `globalThis.fetch` once when the client is built. Resolving it per call costs nothing and
 * means a test that replaces `fetch` is actually observed — without it a test intending to
 * stub the API reaches the network, fails to connect, and the query error looks exactly like
 * a 401, so a signed-out assertion passes whether or not the code under test works. */
export const api = createClient<paths>({
  baseUrl: API_BASE_URL,
  credentials: CREDENTIALS,
  fetch: (request) => globalThis.fetch(request),
});

/** `fetch` against the API with the session cookie attached.
 *
 * The few calls that cannot go through the typed client — the streamed SSE turn, the
 * onboarding gate, the notes endpoints that return raw markdown — go through this instead of
 * raw `fetch`, so there is one place a call can be written and no way to write one that
 * quietly drops the credential. */
export function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(path.startsWith("http") ? path : `${API_BASE_URL}${path}`, {
    ...init,
    credentials: CREDENTIALS,
  });
}
