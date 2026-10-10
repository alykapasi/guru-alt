# Rendering edge cases and accessibility (S53, remainder)

**Status:** approved in conversation 2026-10-09; this document records it.
**Tracker:** S53. Workstream 5, piece 6 (after S47, S49, S37, S17, S62 part A). S62 part B
follows this piece.

## Problem

S53's first part gave chat, notes and item stems one renderer (Markdown, GFM, LaTeX, code,
citations). The tracker row leaves three things open:

1. **Indented code vs LaTeX normalisation.** `normaliseLatexDelimiters`
   (`frontend/src/components/content/latexdelims.ts`) finds code with a regex that sees fenced
   blocks and inline spans but not four-space indented code, so `\(x\)` inside an indented block
   is rewritten to math.
2. **Panel layout.** The chat shell always shows a 288 px conversation sidebar; a citation opens
   a fixed 320 px column; practice has a fixed 320 px question column. Nothing responds to
   width, so at phone width the transcript is squeezed to almost nothing. The top navigation's
   links sit inline and overflow at 375 px.
3. **Keyboard and screen-reader use.**
   - The citation panel's close button has no name, and opening it does not move focus.
   - The delete confirmation is a non-modal `<dialog open>`: no focus trap, Escape does nothing.
   - Conversation-row actions are `opacity-0` until hovered: invisible to a keyboard user and
     unreachable on touch.
   - The chat mode buttons expose no pressed state; the active conversation no current state.
   - Streamed replies are not announced.
   - The chat shell has no `<main>` landmark and no page has a skip link.
   - Nothing checks accessibility automatically.

## Decisions (from the design conversation)

1. **Down to phone width.** Chat, practice and navigation work at 375 px.
2. **axe in the browser tests.** `@axe-core/playwright` (dev dependency) scans the main screens
   and fails on serious or critical violations; keyboard journeys are hand-written. No axe in
   unit tests (jsdom cannot judge contrast or layout).
3. **One `SidePanel` component** carries the responsive and focus behaviour for every side
   panel, rather than daisyUI's checkbox drawer or per-page breakpoints.

## Behaviour

### 1. Layout

**`SidePanel`** (`frontend/src/components/layout/SidePanel.tsx`)

- Props: `open`, `onClose`, `side` (`"left" | "right"`), `label` (accessible name), `width`
  (Tailwind width class for the wide mode), `children`.
- **Wide (≥ 1024 px):** an `<aside aria-label={label}>` column at `width`, as today. Rendered
  only when `open`.
- **Narrow:** a `<dialog>` opened with `showModal()`, a full-height sheet from `side`, at most
  85 % of the viewport wide, with a backdrop. Escape, a backdrop click or its close button call
  `onClose`.
- Mode comes from `useMediaQuery(WIDE_QUERY)` (`frontend/src/hooks/useMediaQuery.ts`), where
  `WIDE_QUERY = "(min-width: 1024px)"` is the one breakpoint constant, matching Tailwind's
  `lg`. CSS alone cannot do this: a modal dialog and an inline column behave differently for
  focus and for assistive technology.

**Chat** (`ChatShell`, `Chat`)

- Conversation sidebar: an inline column when wide. When narrow, a left `SidePanel`, closed by
  default, opened by a "Conversations" button in a small bar above the transcript; choosing a
  conversation or starting a new chat closes it.
- Citation: a right `SidePanel`.
- The transcript column is `<main id="main">`.

**Practice** (`Session`)

- Wide: unchanged — the right column holds the citation above the question panel.
- Narrow: the question is a compact card pinned above the composer (stem clamped to three
  lines) with a "Show question" button that opens the full question panel, rating included, in
  a right `SidePanel`. A citation opens in its own sheet.

**Top navigation** (`NavBar`): below 640 px the links fold into a menu button (a `<details>`
dropdown, keyboard operable); account controls stay visible.

Other pages already sit in a centred column; the axe scan and the phone-width journey catch
anything that overflows.

### 2. Keyboard and screen reader

**Focus**

- `SidePanel` owns it. Narrow: the modal dialog traps focus and the browser returns it to the
  opener. Wide: opening moves focus to the panel heading (`tabIndex={-1}`); closing returns it
  to the element that opened the panel.
- The delete confirmation (`RemovalDialog`) uses `showModal()`, as `NewChatModal` does: trapped
  focus, Escape closes, focus returns.
- A "Skip to content" link is the first focusable element in `PageShell` and `ChatShell`,
  targeting `#main`.

**Visible and named controls**

- Conversation-row actions show on `group-focus-within`, and always on narrow or coarse-pointer
  screens.
- The active conversation row carries `aria-current="page"`; the chat mode buttons carry
  `aria-pressed`.
- Every icon-only button has an `aria-label` (citation close among them); the composer textarea
  has one.

**Announcements**

- The transcript is `role="log"` with `aria-live="off"`, so tokens are not read one by one.
- One visually hidden polite live region (`frontend/src/components/LiveAnnouncer.tsx`)
  announces "Guru is replying…" when a turn starts, then "Reply finished" or the turn's error
  text when it ends.
- A practice grade (`CheckResultCard`) and the paused or ended practice notices are announced
  through the same region.

### 3. Indented code and LaTeX rewriting

- `normaliseLatexDelimiters` parses the source once with `mdast-util-from-markdown` and the GFM
  and math extensions the renderer uses (`micromark-extension-gfm`, `mdast-util-gfm`,
  `micromark-extension-math`, `mdast-util-math`, declared as direct dependencies), collects the
  offsets of every `code` and `inlineCode` node — fenced, indented or inline — and rewrites
  `\(…\)` and `\[…\]` only outside them. The `CODE_REGION` regex is removed.
- Source with no `\(` or `\[` is returned unparsed.
- An unclosed fence mid-stream parses as code to the end of the text, as the regex treated it.

### 4. Tests and verification

**Vitest** (the existing 221 stay green)

- `SidePanel`: wide renders a labelled `aside`; narrow renders a modal dialog; Escape and the
  close button call `onClose`; focus moves in on open and back to the opener on close.
  `matchMedia` is mocked; jsdom lacks `showModal`/`close`, so the test setup shims them.
- `RemovalDialog` opens modally and closes on Escape.
- `ConversationRow`: actions reachable by keyboard; the active row has `aria-current`.
- `Composer`: mode buttons expose `aria-pressed`; the textarea is labelled.
- `LiveAnnouncer`: one announcement each for start, finish and error; none per token.
- `Session` narrow: the pinned question card renders.
- `latexdelims`: `\(x\)` in an indented block after a blank line stays literal; list
  continuation indented four spaces is still rewritten; a tab-indented block stays literal;
  fenced and inline cases unchanged; multi-line `\[…\]` outside code rewritten.

**Playwright**

- `e2e/a11y.ts`: `expectAccessible(page)` runs axe with the `wcag2a`, `wcag2aa`, `wcag21a`,
  `wcag21aa` tags and fails on `serious` or `critical` violations, listing rule and target. The
  existing journeys call it on chat, practice, the wizard, library and admin.
- `e2e/a11y.spec.ts`, under a `desktop` (1280×800) and a `phone` (390×844) project; the other
  journeys stay desktop-only:
  - Keyboard chat: skip link, composer, send (FakeProvider), open a citation marker by keyboard,
    focus lands in the panel, Escape returns it to the marker.
  - Phone chat: the conversations drawer opens and closes, choosing a conversation closes it,
    and `document.documentElement.scrollWidth ≤ window.innerWidth`.
  - Phone practice: the question card is visible above the composer; "Show question" opens the
    panel and rating is reachable.
  - Phone navigation: the menu button reveals the links.

**Gates:** `npm run build`, `npm test`, `npm run lint`, `npm run e2e`. No paid model calls.

**Docs:** tracker S53 → Completed; RUNBOOK gains a short section on the axe gate and reading its
output.

## Out of scope

- Manual screen-reader passes (VoiceOver, NVDA): recommended before launch, recorded in the
  tracker.
- Colour contrast beyond what axe reports; per-renderer math speech (KaTeX already emits
  MathML).
- Pages other than chat, practice and navigation, except where axe reports a violation — those
  are fixed in this piece.
