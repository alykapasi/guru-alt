# Guru frontend

React + TypeScript + Vite. See `docs/ROADMAP.md` (Phase 7) and `docs/MASTERPLAN.md` in the repo
root for product direction; this README only covers running the app.

## Setup

```bash
npm install
```

Requires the backend running (see the repo root `README.md`) — the dev server defaults to
`http://localhost:8000`; override with `VITE_API_BASE_URL` in a `.env.local` if it runs elsewhere.

## Commands

- `npm run dev` — Vite dev server (`http://localhost:5173`)
- `npm run build` — type-check + production build
- `npm run lint` — ESLint + Prettier check
- `npm run format` — Prettier write
- `npm run gen:api` — regenerate `src/api/schema.d.ts` from the backend's live OpenAPI schema
  (backend must be running)

## Layout

- `src/api/` — the typed REST client (`client.ts`, generated `schema.d.ts`) and the hand-rolled
  SSE stream reader (`sse.ts`) for the chat endpoints, which aren't representable in OpenAPI.
- `src/theme/` — the two custom DaisyUI themes (`guru-light`/`guru-dark`) and the type scale.
  Built entirely from custom tokens, not a stock DaisyUI theme.
- `src/components/` — shared primitives (nav, logo, theme toggle, page shell).
- `src/pages/` — one file per top-level route.
