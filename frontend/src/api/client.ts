import createClient from "openapi-fetch";
import type { paths } from "./schema";

/** The base URL the typed client and the hand-rolled SSE helper both target. Dev default
 * matches app/core/config.py's cors_origins counterpart (the FastAPI dev server). */
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

/** Fully-typed request client generated from the backend's OpenAPI schema (see
 * `npm run gen:api`). Every REST endpoint should go through this, not a raw fetch. */
export const api = createClient<paths>({ baseUrl: API_BASE_URL });
