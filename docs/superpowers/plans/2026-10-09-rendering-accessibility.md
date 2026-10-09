# Rendering edge cases and accessibility (S53) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chat, practice and navigation work at phone width and by keyboard and screen reader, indented code is never rewritten as LaTeX, and an axe scan in the browser journeys keeps it that way.

**Architecture:** One `SidePanel` component (inline `<aside>` at ≥1024 px, native modal `<dialog>` sheet below) carries the responsive and focus behaviour for the conversation sidebar, the citation pane and the practice question panel. A `useTurnAnnouncement` hook feeds one polite live region. The LaTeX rewrite asks the same Markdown parser the renderer uses where code is. `@axe-core/playwright` scans the main screens in the existing journeys, and a new `a11y.spec.ts` drives keyboard and phone flows.

**Tech Stack:** React 19, TypeScript, Tailwind 4 + daisyUI 5, Vitest + Testing Library (jsdom), Playwright, `mdast-util-from-markdown`, `@axe-core/playwright`.

**Spec:** `docs/superpowers/specs/2026-10-09-rendering-accessibility-design.md`

## Global Constraints

- Branch `feat/workstream-2` (PR #44). Never reset, amend, rebase, squash or force-push.
- One tracker id per commit subject: `[S53]`. Trailer exactly `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Stage only the task's files by name, then run `git status`.
- The wide breakpoint is one constant: `WIDE_QUERY = "(min-width: 1024px)"` (Tailwind `lg`).
- Phone width target: 375 px; the Playwright phone project is 390×844, desktop 1280×800.
- axe tags `wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa`; fail on impact `serious` or `critical`.
- Raw HTML stays disabled in the renderer (no `rehype-raw`).
- No paid model calls: browser journeys run on `scripts/e2e-backend.sh` (the `shaped` provider).
- Frontend gates: `npm run build` (the real type gate — `tsc --noEmit` checks nothing), `npm test`, `npm run lint`. `npm run e2e` at Tasks 8 and 9.
- Run frontend commands from `frontend/`.

## Review Focus

1. **A citation clicked on a phone while a sheet is already open** (e.g. the practice question sheet): the citation sheet must stack on top and Escape must close only the top one. Pinned in Task 4 (`Session` test: citation opens while the question sheet is open).
2. **Resizing across 1024 px with a panel open** (rotate a tablet): the panel must not be lost or left as an orphan modal. `SidePanel` closes its dialog when it leaves narrow mode. Pinned in Task 2.
3. **An error that arrives while the reply is streaming**: the announcement must be the error text, not "Reply finished". Pinned in Task 7.
4. **`\(` inside a list item's four-space continuation line** must still render as math, while an indented code block after a blank line stays literal. Pinned in Task 1.
5. **The conversation drawer on a phone after "New chat"**: the new-chat modal opens over the drawer, and starting a chat navigates and closes the drawer. Pinned in Task 3 (route change closes the drawer) and Task 8 (phone journey).

## File Structure

- Create `frontend/src/hooks/useMediaQuery.ts`: `WIDE_QUERY`, `useMediaQuery(query)`.
- Create `frontend/src/components/layout/SidePanel.tsx`: responsive panel.
- Create `frontend/src/components/layout/SkipLink.tsx`: "Skip to content".
- Create `frontend/src/components/LiveAnnouncer.tsx`: `LiveAnnouncer`, `useTurnAnnouncement`, `gradeAnnouncement`.
- Create `frontend/src/components/lessons/QuestionCard.tsx`: the pinned narrow-screen question.
- Create `frontend/src/components/content/latexdelims.test.ts`.
- Create `frontend/e2e/a11y.ts`, `frontend/e2e/a11y.spec.ts`.
- Modify `latexdelims.ts`, `src/test/setup.ts`, `ChatShell.tsx`, `ConversationSidebar.tsx`, `ConversationRow.tsx`, `CitationPane.tsx`, `Chat.tsx`, `Session.tsx`, `SessionShell.tsx`, `PageShell.tsx`, `NavBar.tsx`, `Composer.tsx`, `RemovalDialog.tsx`, `MessageList.tsx`, `playwright.config.ts`, the five existing e2e specs, `package.json`, `docs/RUNBOOK.md`, `docs/guru-suggestions-tracker.md`.

---

### Task 1: Indented code is never rewritten as LaTeX

**Files:**
- Modify: `frontend/src/components/content/latexdelims.ts`
- Modify: `frontend/package.json` (declare the parser packages)
- Test: `frontend/src/components/content/latexdelims.test.ts`

**Interfaces:**
- Produces: `normaliseLatexDelimiters(source: string): string` (unchanged signature).

- [ ] **Step 1: Write the failing tests**

```ts
// frontend/src/components/content/latexdelims.test.ts
import { describe, expect, it } from "vitest";
import { normaliseLatexDelimiters } from "./latexdelims";

/** What the rewrite may touch is decided by the Markdown parser, not a pattern: four spaces of
 * indentation are code after a blank line and ordinary continuation text inside a list item. */
describe("normaliseLatexDelimiters", () => {
  it("leaves an indented code block after a blank line alone", () => {
    const source = "Some prose.\n\n    \\(x\\) stays literal\n";
    expect(normaliseLatexDelimiters(source)).toBe(source);
  });

  it("leaves a tab-indented code block alone", () => {
    const source = "Prose.\n\n\t\\[y\\]\n";
    expect(normaliseLatexDelimiters(source)).toBe(source);
  });

  it("still rewrites a list item's indented continuation", () => {
    const source = "1. First point\n\n    where \\(x > 0\\) holds\n";
    expect(normaliseLatexDelimiters(source)).toBe("1. First point\n\n    where $x > 0$ holds\n");
  });

  it("leaves fenced and inline code alone", () => {
    const source = "```tex\n\\(a\\)\n```\nand `\\(b\\)` but \\(c\\)";
    expect(normaliseLatexDelimiters(source)).toBe("```tex\n\\(a\\)\n```\nand `\\(b\\)` but $c$");
  });

  it("rewrites display math across lines outside code", () => {
    expect(normaliseLatexDelimiters("\\[\na + b\n\\]")).toBe("$$a + b$$");
  });

  it("treats an unclosed fence mid-stream as code to the end", () => {
    const source = "```\n\\(x\\)";
    expect(normaliseLatexDelimiters(source)).toBe(source);
  });

  it("returns text with no delimiters untouched", () => {
    expect(normaliseLatexDelimiters("    plain")).toBe("    plain");
  });
});
```

- [ ] **Step 2: Run them to verify the indented and tab cases fail**

Run: `npm test -- src/components/content/latexdelims.test.ts`
Expected: FAIL on "leaves an indented code block…" and "leaves a tab-indented code block…" (output contains `$x$` / `$$y$$`); the rest pass.

- [ ] **Step 3: Replace the regex with the parser**

```ts
// frontend/src/components/content/latexdelims.ts
import { fromMarkdown } from "mdast-util-from-markdown";
import { gfm } from "micromark-extension-gfm";
import { gfmFromMarkdown } from "mdast-util-gfm";
import { math } from "micromark-extension-math";
import { mathFromMarkdown } from "mdast-util-math";

const INLINE = /\\\((.+?)\\\)/g;
const DISPLAY = /\\\[([\s\S]+?)\\\]/g;

/** Nodes whose text the learner is meant to read exactly as written. */
const VERBATIM = new Set(["code", "inlineCode", "math", "inlineMath"]);

interface Node {
  type: string;
  position?: { start: { offset?: number }; end: { offset?: number } };
  children?: Node[];
}

function rewrite(prose: string): string {
  return prose
    .replace(DISPLAY, (_, body: string) => `$$${body.trim()}$$`)
    .replace(INLINE, (_, body: string) => `$${body.trim()}$`);
}

/** Source ranges of every code and math node, in document order. */
function verbatimRanges(source: string): Array<[number, number]> {
  const tree = fromMarkdown(source, {
    extensions: [gfm(), math()],
    mdastExtensions: [gfmFromMarkdown(), mathFromMarkdown()],
  }) as Node;
  const ranges: Array<[number, number]> = [];
  const walk = (node: Node) => {
    const start = node.position?.start.offset;
    const end = node.position?.end.offset;
    if (VERBATIM.has(node.type) && start !== undefined && end !== undefined) {
      ranges.push([start, end]);
      return;
    }
    node.children?.forEach(walk);
  };
  walk(tree);
  return ranges;
}

/**
 * Rewrites LaTeX's `\(x\)` and `\[x\]` delimiters to the `$x$` and `$$x$$` remark-math parses.
 *
 * Models emit this pair constantly — it is what LaTeX itself prescribes — and without this an
 * otherwise correct derivation arrives with `\(2\times2\)` sitting in the prose as literal
 * backslashes.
 *
 * It has to happen here, on the source, rather than as a plugin over the parsed tree: `\(` is a
 * CommonMark escape for a literal paren, so by the time any plugin runs the backslashes are
 * gone and `(x)` is indistinguishable from ordinary parentheses. Converting to `$` rather than
 * to some sentinel of our own matters for the same reason in reverse — remark-math parses the
 * body with its own micromark extension, so `a_1` inside stays a subscript instead of being
 * read as Markdown emphasis.
 *
 * Where code is, the parser decides (S53): four spaces of indentation are a code block after a
 * blank line and ordinary continuation text inside a list item, which no pattern can tell
 * apart. Text with no delimiter is returned without parsing.
 */
export function normaliseLatexDelimiters(source: string): string {
  if (!source.includes("\\(") && !source.includes("\\[")) return source;
  let out = "";
  let cut = 0;
  for (const [start, end] of verbatimRanges(source)) {
    out += rewrite(source.slice(cut, start)) + source.slice(start, end);
    cut = end;
  }
  return out + rewrite(source.slice(cut));
}
```

Add to `frontend/package.json` `dependencies` (versions already installed transitively):

```json
"mdast-util-from-markdown": "^2.0.3",
"mdast-util-gfm": "^3.1.0",
"mdast-util-math": "^3.0.0",
"micromark-extension-gfm": "^3.0.0",
"micromark-extension-math": "^3.1.0",
```

Then run `npm install` so `package-lock.json` records them as direct dependencies.

- [ ] **Step 4: Run the new and the existing renderer tests**

Run: `npm test -- src/components/content`
Expected: PASS, including `RichTextContent.test.tsx`'s "LaTeX's other delimiters" block.

- [ ] **Step 5: Build, lint, commit**

Run: `npm run build && npm run lint`
Expected: both exit 0.

```bash
git add frontend/src/components/content/latexdelims.ts frontend/src/components/content/latexdelims.test.ts frontend/package.json frontend/package-lock.json
git commit -m "fix(frontend): the Markdown parser decides where code is before LaTeX is rewritten [S53]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 2: `SidePanel` and `useMediaQuery`

**Files:**
- Create: `frontend/src/hooks/useMediaQuery.ts`
- Create: `frontend/src/components/layout/SidePanel.tsx`
- Modify: `frontend/src/test/setup.ts` (dialog shim; controllable `matchMedia`)
- Test: `frontend/src/components/layout/SidePanel.test.tsx`

**Interfaces:**
- Produces:
  - `WIDE_QUERY: "(min-width: 1024px)"`; `useMediaQuery(query: string): boolean`.
  - `SidePanel(props: { open: boolean; onClose: () => void; side: "left" | "right"; label: string; width: string; showClose?: boolean; children: ReactNode })`. Wide: `<aside aria-label={label} className={width …}>` rendered only when `open`. Narrow: a `<dialog aria-label={label}>` that is always mounted (children stay mounted), opened with `showModal()` when `open`, closed otherwise; `cancel` (Escape) and the native `close` event call `onClose`; a backdrop click calls `onClose`; when `showClose` (default `true`) a button labelled `Close ${label}` calls `onClose`.
  - Test helper in `setup.ts`: `setViewportWide(wide: boolean)` exported from `src/test/viewport.ts`.

- [ ] **Step 1: Shim the dialog and make `matchMedia` controllable**

jsdom has no `showModal`/`close` (checked: both `undefined`). Append to `frontend/src/test/setup.ts`:

```ts
// jsdom implements <dialog> as a plain element: no showModal, no close. The shim does what the
// browser does to the markup — the `open` attribute, and `close` firing a "close" event — so a
// test can assert that a dialog opened and that closing it reached the component.
HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
  this.setAttribute("open", "");
  this.dataset.modal = "true";
};
HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
  if (!this.hasAttribute("open")) return;
  this.removeAttribute("open");
  delete this.dataset.modal;
  this.dispatchEvent(new Event("close"));
};
```

Replace the existing `window.matchMedia` stub's `matches: false` with a lookup, keeping its comment:

```ts
import { viewport } from "./viewport";
// ...
window.matchMedia = ((query: string) => ({
  matches: query === "(min-width: 1024px)" ? viewport.wide : false,
  // (rest unchanged)
```

```ts
// frontend/src/test/viewport.ts
/** Which side of the one layout breakpoint a test renders on. Narrow by default — the setup
 * file's "no preference" stance — and reset after every test so one file's choice does not
 * leak into the next. */
export const viewport = { wide: false };

export function setViewportWide(wide: boolean): void {
  viewport.wide = wide;
}

afterEach(() => {
  viewport.wide = false;
});
```

(`afterEach` is a global — `globals: true` in `vite.config.ts`.)

- [ ] **Step 2: Write the failing tests**

```tsx
// frontend/src/components/layout/SidePanel.test.tsx
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { SidePanel } from "./SidePanel";
import { setViewportWide } from "../../test/viewport";

function panel(open: boolean, onClose = vi.fn()) {
  return render(
    <SidePanel open={open} onClose={onClose} side="right" label="Source" width="w-80">
      <p>passage</p>
    </SidePanel>,
  );
}

describe("SidePanel, wide", () => {
  it("is a labelled column while open and absent while closed", () => {
    setViewportWide(true);
    const { rerender } = panel(true);
    expect(screen.getByRole("complementary", { name: "Source" })).toHaveTextContent("passage");
    rerender(
      <SidePanel open={false} onClose={() => {}} side="right" label="Source" width="w-80">
        <p>passage</p>
      </SidePanel>,
    );
    expect(screen.queryByRole("complementary")).toBeNull();
  });
});

describe("SidePanel, narrow", () => {
  it("opens as a modal dialog", () => {
    panel(true);
    const dialog = screen.getByRole("dialog", { name: "Source" });
    expect(dialog).toHaveAttribute("data-modal", "true");
  });

  it("calls onClose on Escape, on its close button, and on the backdrop", () => {
    const onClose = vi.fn();
    panel(true, onClose);
    const dialog = screen.getByRole("dialog", { name: "Source" });
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    fireEvent.click(screen.getByRole("button", { name: "Close Source" }));
    fireEvent.click(dialog); // a click whose target is the dialog itself is the backdrop
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it("closes the dialog when open turns false", () => {
    const { rerender } = panel(true);
    rerender(
      <SidePanel open={false} onClose={() => {}} side="right" label="Source" width="w-80">
        <p>passage</p>
      </SidePanel>,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps its children mounted while closed", () => {
    panel(false);
    expect(screen.getByText("passage", { selector: "p" })).toBeInTheDocument();
  });

  it("hides the close button when the content brings its own", () => {
    render(
      <SidePanel open onClose={() => {}} side="right" label="Source" width="w-80" showClose={false}>
        <p>passage</p>
      </SidePanel>,
    );
    expect(screen.queryByRole("button", { name: "Close Source" })).toBeNull();
  });
});

describe("SidePanel across the breakpoint", () => {
  it("does not leave a modal open after the viewport turns wide", () => {
    const { rerender } = panel(true);
    setViewportWide(true);
    rerender(
      <SidePanel open onClose={() => {}} side="right" label="Source" width="w-80">
        <p>passage</p>
      </SidePanel>,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("complementary", { name: "Source" })).toBeInTheDocument();
  });
});
```

The last test rerenders after flipping the viewport; `useMediaQuery` reads `matchMedia(query).matches` through `useSyncExternalStore`, whose snapshot is re-read on render, so the rerender sees the new value.

- [ ] **Step 3: Run them to verify they fail**

Run: `npm test -- src/components/layout`
Expected: FAIL — `Failed to resolve import "./SidePanel"`.

- [ ] **Step 4: Implement**

```ts
// frontend/src/hooks/useMediaQuery.ts
import { useSyncExternalStore } from "react";

/** The one layout breakpoint (Tailwind's `lg`). Above it side panels sit beside the content;
 * below it they open as sheets over it (S53). */
export const WIDE_QUERY = "(min-width: 1024px)";

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    () => window.matchMedia(query).matches,
  );
}
```

```tsx
// frontend/src/components/layout/SidePanel.tsx
import { useEffect, useRef, type ReactNode } from "react";
import { X } from "lucide-react";
import { useMediaQuery, WIDE_QUERY } from "../../hooks/useMediaQuery";

/** A panel beside the content on a wide screen and a sheet over it on a narrow one (S53).
 *
 * The narrow form is a native modal dialog on purpose: the browser then traps focus inside
 * it, closes it on Escape, returns focus to whatever opened it, and hides the page behind it
 * from assistive technology — four things a hand-rolled drawer gets wrong one at a time.
 * Its children stay mounted while it is closed, so a half-revealed flashcard or a scrolled
 * list is where the learner left it when the sheet opens again. */
export function SidePanel({
  open,
  onClose,
  side,
  label,
  width,
  showClose = true,
  children,
}: {
  open: boolean;
  onClose: () => void;
  side: "left" | "right";
  label: string;
  /** Tailwind width class for the wide column, e.g. "w-80". */
  width: string;
  /** False when the content carries its own close control. */
  showClose?: boolean;
  children: ReactNode;
}) {
  const wide = useMediaQuery(WIDE_QUERY);
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    else if (!open && dialog.open) dialog.close();
  }, [open, wide]);

  if (wide) {
    if (!open) return null;
    const edge = side === "left" ? "border-r" : "border-l";
    return (
      <aside
        aria-label={label}
        className={`border-base-300 bg-base-100 flex ${width} min-h-0 shrink-0 flex-col ${edge}`}
      >
        {children}
      </aside>
    );
  }

  const placement = side === "left" ? "mr-auto" : "ml-auto";
  return (
    <dialog
      ref={dialogRef}
      aria-label={label}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClose={() => {
        if (open) onClose();
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      className={`bg-base-100 m-0 ${placement} h-svh max-h-svh w-[85vw] max-w-sm p-0 backdrop:bg-black/40`}
    >
      <div className="flex h-full min-h-0 flex-col">
        {showClose && (
          <div className="border-base-300 flex justify-end border-b p-2">
            <button
              type="button"
              onClick={onClose}
              aria-label={`Close ${label}`}
              className="hover:bg-base-200 rounded-field p-1.5"
            >
              <X size={16} />
            </button>
          </div>
        )}
        {children}
      </div>
    </dialog>
  );
}
```

- [ ] **Step 5: Run the tests and the whole unit suite**

Run: `npm test`
Expected: PASS, all files (221 + the new ones). If any existing test now fails because the narrow default changed nothing it relied on, investigate — no existing component reads `useMediaQuery` yet.

- [ ] **Step 6: Build, lint, commit**

Run: `npm run build && npm run lint` — Expected: exit 0.

```bash
git add frontend/src/hooks/useMediaQuery.ts frontend/src/components/layout/SidePanel.tsx frontend/src/components/layout/SidePanel.test.tsx frontend/src/test/setup.ts frontend/src/test/viewport.ts
git commit -m "feat(frontend): one side panel that is a column when wide and a modal sheet when narrow [S53]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 3: Chat layout — conversation drawer, citation panel, row actions

**Files:**
- Modify: `frontend/src/components/chat/ChatShell.tsx`
- Modify: `frontend/src/components/chat/ConversationSidebar.tsx`
- Modify: `frontend/src/components/chat/ConversationRow.tsx`
- Modify: `frontend/src/components/chat/CitationPane.tsx`
- Modify: `frontend/src/pages/Chat.tsx`
- Test: `frontend/src/components/chat/ChatShell.test.tsx`, `frontend/src/components/chat/ConversationRow.test.tsx`, `frontend/src/components/chat/CitationPane.test.tsx` (extend)

**Interfaces:**
- Consumes: `SidePanel`, `useMediaQuery`, `WIDE_QUERY`, `setViewportWide` (Task 2).
- Produces:
  - `ConversationSidebar` renders only its contents (a `div`), no `<aside>`; `ChatShell` wraps it.
  - `CitationPane` focuses its heading on mount and restores focus to the previously focused element on unmount; its close button is labelled "Close source".
  - The transcript column in `Chat` is `<main id="main">`.

- [ ] **Step 1: Write the failing tests**

```tsx
// frontend/src/components/chat/ChatShell.test.tsx
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { ChatShell } from "./ChatShell";
import { setViewportWide } from "../../test/viewport";

vi.mock("../NavBar", () => ({ NavBar: () => null }));
vi.mock("../ImpersonationBanner", () => ({ ImpersonationBanner: () => null }));
vi.mock("./ConversationSidebar", () => ({
  ConversationSidebar: () => <p>conversation list</p>,
}));

function Go() {
  const navigate = useNavigate();
  return <button onClick={() => navigate("/app/chat/abc")}>go</button>;
}

function shell() {
  return render(
    <MemoryRouter initialEntries={["/app/chat"]}>
      <Routes>
        <Route path="/app/chat" element={<ChatShell />}>
          <Route index element={<Go />} />
          <Route path=":conversationId" element={<p>conversation</p>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("ChatShell", () => {
  it("shows the conversation list as a column when wide, with no drawer button", () => {
    setViewportWide(true);
    shell();
    expect(screen.getByRole("complementary", { name: "Conversations" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Conversations" })).toBeNull();
  });

  it("puts the list in a drawer when narrow, closed until asked for", () => {
    shell();
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Conversations" }));
    expect(screen.getByRole("dialog", { name: "Conversations" })).toHaveAttribute("data-modal");
  });

  it("closes the drawer when the route changes", () => {
    shell();
    fireEvent.click(screen.getByRole("button", { name: "Conversations" }));
    fireEvent.click(screen.getByRole("button", { name: "go" }));
    expect(screen.getByText("conversation")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("offers a skip link to the content", () => {
    shell();
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main");
  });
});
```

```tsx
// frontend/src/components/chat/ConversationRow.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ConversationRow } from "./ConversationRow";

vi.mock("../../api/hooks", () => ({
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useArchive: () => ({ mutate: vi.fn() }),
  useRemovalImpact: () => ({ data: undefined, isLoading: false }),
  useRemove: () => ({ mutate: vi.fn(), isPending: false, error: null }),
}));

const conversation = {
  id: "c1",
  title: "Eigenvalues",
  goal: null,
  kind: "chat",
} as unknown as Parameters<typeof ConversationRow>[0]["conversation"];

describe("ConversationRow", () => {
  it("marks the open conversation as the current page", () => {
    render(
      <MemoryRouter>
        <ConversationRow conversation={conversation} active />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "Eigenvalues" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("shows its actions to keyboard focus and on touch or narrow screens, not only on hover", () => {
    render(
      <MemoryRouter>
        <ConversationRow conversation={conversation} active={false} />
      </MemoryRouter>,
    );
    const actions = screen.getByRole("button", { name: "Rename conversation" }).parentElement!;
    expect(actions.className).toContain("group-focus-within:opacity-100");
    expect(actions.className).toContain("max-lg:opacity-100");
    expect(actions.className).toContain("pointer-coarse:opacity-100");
  });
});
```

Append to `frontend/src/components/chat/CitationPane.test.tsx` (read the file first and reuse its existing hook mocks for `useChunk`/`useSource`):

```tsx
describe("CitationPane focus", () => {
  it("takes focus when it opens and gives it back when it closes", () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    const { unmount } = render(<CitationPane citation={citation} onClose={() => {}} />);
    expect(screen.getByRole("heading", { name: "Source" })).toHaveFocus();
    unmount();
    expect(opener).toHaveFocus();
    opener.remove();
  });

  it("names its close button", () => {
    render(<CitationPane citation={citation} onClose={() => {}} />);
    expect(screen.getByRole("button", { name: "Close source" })).toBeInTheDocument();
  });
});
```

(`citation` is the fixture the existing file already defines; if it is named differently, use that name.)

- [ ] **Step 2: Run them to verify they fail**

Run: `npm test -- src/components/chat/ChatShell.test.tsx src/components/chat/ConversationRow.test.tsx src/components/chat/CitationPane.test.tsx`
Expected: FAIL — no complementary named "Conversations", no "Conversations" button, no skip link, no `aria-current`, missing classes, heading not focused, no "Close source" button.

- [ ] **Step 3: Implement**

`ConversationSidebar.tsx`: change the outer `<aside className="border-base-300 bg-base-100 flex w-72 shrink-0 flex-col border-r">` to `<div className="flex min-h-0 flex-1 flex-col">` (and its closing tag). The panel chrome now belongs to `SidePanel`.

`ChatShell.tsx`:

```tsx
import { useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { PanelLeft } from "lucide-react";
import { ImpersonationBanner } from "../ImpersonationBanner";
import { NavBar } from "../NavBar";
import { SkipLink } from "../layout/SkipLink";
import { SidePanel } from "../layout/SidePanel";
import { useMediaQuery, WIDE_QUERY } from "../../hooks/useMediaQuery";
import { ConversationSidebar } from "./ConversationSidebar";

/** Full-height two-pane layout for the chat route — breaks out of PageShell's centered
 * max-w-[1280px] content column, since a sidebar + transcript needs the full viewport
 * width/height below the nav, not a centered text column. Below the wide breakpoint the
 * conversation list is a drawer, so the transcript keeps the whole width (S53). */
export function ChatShell() {
  const wide = useMediaQuery(WIDE_QUERY);
  const { pathname } = useLocation();
  const [drawer, setDrawer] = useState<{ open: boolean; at: string }>({ open: false, at: pathname });
  // Choosing a conversation, or starting one, is a navigation: the drawer has done its job.
  const drawerOpen = drawer.open && drawer.at === pathname;

  return (
    <div className="bg-base-100 flex h-svh flex-col">
      <SkipLink />
      {/* This route is the likeliest reason to be viewing an account at all, so it is the
          likeliest place to forget that you are (P10). */}
      <ImpersonationBanner />
      <NavBar />
      {!wide && (
        <div className="border-base-300 flex items-center border-b px-4 py-2">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => setDrawer({ open: true, at: pathname })}
          >
            <PanelLeft size={16} />
            Conversations
          </button>
        </div>
      )}
      <div className="flex min-h-0 flex-1">
        <SidePanel
          open={wide || drawerOpen}
          onClose={() => setDrawer({ open: false, at: pathname })}
          side="left"
          label="Conversations"
          width="w-72"
        >
          <ConversationSidebar />
        </SidePanel>
        <Outlet />
      </div>
    </div>
  );
}
```

(Keying the open state on the path it was opened at closes the drawer on navigation without an effect that sets state.)

`frontend/src/components/layout/SkipLink.tsx` (also used by Task 5):

```tsx
/** The first thing a keyboard user reaches: past the navigation to the page's own content. */
export function SkipLink() {
  return (
    <a
      href="#main"
      className="focus:bg-base-100 focus:text-primary sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded-field focus:px-3 focus:py-2"
    >
      Skip to content
    </a>
  );
}
```

`ConversationRow.tsx`: on the `Link`, add `aria-current={active ? "page" : undefined}`. On the actions `div`, replace `opacity-0 transition-opacity group-hover:opacity-100` with `opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 max-lg:opacity-100 pointer-coarse:opacity-100`.

`CitationPane.tsx`: add focus handling and the label:

```tsx
import { useEffect, useRef } from "react";
// ...
export function CitationPane({ citation, onClose }: { citation: Citation; onClose: () => void }) {
  // ...existing hooks...
  const headingRef = useRef<HTMLHeadingElement>(null);

  // Opening a citation is a request to read it: focus goes to the pane, and back to the marker
  // that opened it when the pane closes, so a keyboard user is not dropped at the top of the
  // page (S53). On a narrow screen the sheet's dialog does the same for itself.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    headingRef.current?.focus();
    return () => opener?.focus();
  }, []);
```

and in the markup: `<h3 ref={headingRef} tabIndex={-1} className="text-h3 flex items-center gap-2 outline-none">`, and on the close button add `type="button" aria-label="Close source"`.

`Chat.tsx`:
- The transcript column `<div className="flex min-h-0 flex-1 flex-col">` becomes `<main id="main" className="flex min-h-0 min-w-0 flex-1 flex-col">` (closing tag too).
- Replace the trailing `{citation && (<aside …><CitationPane …/></aside>)}` with:

```tsx
<SidePanel
  open={citation !== null}
  onClose={() => setCitation(null)}
  side="right"
  label="Source"
  width="w-80"
  showClose={false}
>
  {citation && <CitationPane citation={citation} onClose={() => setCitation(null)} />}
</SidePanel>
```

with `import { SidePanel } from "../components/layout/SidePanel";`.

- [ ] **Step 4: Run the tests and the unit suite**

Run: `npm test`
Expected: PASS.

- [ ] **Step 5: Build, lint, commit**

Run: `npm run build && npm run lint` — Expected: exit 0.

```bash
git add frontend/src/components/chat/ChatShell.tsx frontend/src/components/chat/ChatShell.test.tsx frontend/src/components/chat/ConversationSidebar.tsx frontend/src/components/chat/ConversationRow.tsx frontend/src/components/chat/ConversationRow.test.tsx frontend/src/components/chat/CitationPane.tsx frontend/src/components/chat/CitationPane.test.tsx frontend/src/components/layout/SkipLink.tsx frontend/src/pages/Chat.tsx
git commit -m "feat(frontend): chat fits a phone — the conversation list is a drawer and a citation a sheet [S53]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 4: Practice layout — pinned question card and question sheet

**Files:**
- Create: `frontend/src/components/lessons/QuestionCard.tsx`
- Modify: `frontend/src/pages/Session.tsx`, `frontend/src/components/lessons/SessionShell.tsx`
- Test: `frontend/src/pages/Session.test.tsx` (extend)

**Interfaces:**
- Consumes: `SidePanel`, `useMediaQuery`, `WIDE_QUERY`, `setViewportWide`, `SkipLink` (Tasks 2–3).
- Produces: `QuestionCard({ item, onShow }: { item: ItemEvent | null; onShow: () => void })`.

- [ ] **Step 1: Write the failing tests**

Read `Session.test.tsx` first; reuse its `conversation(...)` helper and its `render` wrapper. Add `vi.mock("../components/chat/CitationPane", () => ({ CitationPane: () => <p>citation</p> }));` if `CitationPane` is not already mocked, and set `item: { id: "i1", item_type: "free_response", stem: "What is an eigenvalue?", kcs: [] }` in the state these tests use. Then append:

```tsx
describe("Session layout", () => {
  it("keeps the question in the right-hand column when wide", () => {
    setViewportWide(true);
    renderSession();
    expect(screen.getByRole("complementary", { name: "Practice question" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show question" })).toBeNull();
  });

  it("pins the question above the composer when narrow, with the full panel a tap away", () => {
    renderSession();
    expect(screen.getByText("What is an eigenvalue?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show question" }));
    expect(screen.getByRole("dialog", { name: "Practice question" })).toHaveAttribute("data-modal");
  });

  it("stacks a citation sheet over an open question sheet", () => {
    renderSession();
    fireEvent.click(screen.getByRole("button", { name: "Show question" }));
    act(() => chat.openCitation?.({ marker: 1, chunk_id: "k", source_id: "s" }));
    expect(screen.getByRole("dialog", { name: "Source" })).toHaveAttribute("data-modal");
    expect(screen.getByRole("dialog", { name: "Practice question" })).toHaveAttribute("data-modal");
  });
});
```

To reach `setCitation` from the test, change the existing `MessageList` mock to capture its prop:

```tsx
vi.mock("../components/chat/MessageList", () => ({
  MessageList: ({ onCitationClick }: { onCitationClick: (c: unknown) => void }) => {
    chat.openCitation = onCitationClick;
    return null;
  },
}));
```

and widen the hoisted state: `const chat = vi.hoisted(() => ({ state: {} as Record<string, unknown>, openCitation: undefined as undefined | ((c: never) => void) }));`. `renderSession` is the file's existing render wrapper (use its real name). Import `act`, `fireEvent` from `@testing-library/react` and `setViewportWide` from `../test/viewport`.

Existing Session tests run narrow by default; the `item-panel` test id stays findable because the narrow sheet keeps its children mounted (Task 2).

- [ ] **Step 2: Run them to verify they fail**

Run: `npm test -- src/pages/Session.test.tsx`
Expected: FAIL — no complementary named "Practice question", no "Show question" button.

- [ ] **Step 3: Implement**

```tsx
// frontend/src/components/lessons/QuestionCard.tsx
import { RichText } from "../content/RichText";
import type { ItemEvent } from "../../api/sse";

/** The question, kept in view above the composer on a narrow screen (S53). A learner answering
 * must be able to see what they are answering; the full panel, with its rating controls, is one
 * tap away rather than beside the transcript where it no longer fits. */
export function QuestionCard({ item, onShow }: { item: ItemEvent | null; onShow: () => void }) {
  return (
    <div className="border-base-300 bg-base-200/40 flex items-start gap-3 border-t px-4 py-3">
      <div className="line-clamp-3 min-w-0 flex-1">
        {item ? (
          <RichText content={item.stem} className="text-base-content/90" />
        ) : (
          <p className="text-caption text-base-content/50">Preparing your practice…</p>
        )}
      </div>
      <button type="button" className="btn btn-ghost btn-xs shrink-0" onClick={onShow}>
        Show question
      </button>
    </div>
  );
}
```

`Session.tsx`:
- Add `const wide = useMediaQuery(WIDE_QUERY);` and `const [questionOpen, setQuestionOpen] = useState(false);`.
- The transcript column becomes `<main id="main" className="flex min-h-0 min-w-0 flex-1 flex-col">`.
- Directly above `<Composer …/>`, add `{!wide && <QuestionCard item={item} onShow={() => setQuestionOpen(true)} />}`.
- Replace the trailing `<aside …>…</aside>` with:

```tsx
{/* Evidence stacks above the question rather than replacing it: a learner opening a
    citation is checking a source *in order to answer*, so hiding the item they are
    answering to show it would defeat the click. On a narrow screen each is its own sheet,
    and the citation's opens over the question's (S53). */}
{wide ? (
  <aside
    aria-label="Practice question"
    className="border-base-300 divide-base-300 flex w-80 shrink-0 flex-col divide-y border-l"
  >
    {citation && <CitationPane citation={citation} onClose={() => setCitation(null)} />}
    {itemPanel}
  </aside>
) : (
  <>
    <SidePanel
      open={questionOpen}
      onClose={() => setQuestionOpen(false)}
      side="right"
      label="Practice question"
      width="w-80"
    >
      {itemPanel}
    </SidePanel>
    <SidePanel
      open={citation !== null}
      onClose={() => setCitation(null)}
      side="right"
      label="Source"
      width="w-80"
      showClose={false}
    >
      {citation && <CitationPane citation={citation} onClose={() => setCitation(null)} />}
    </SidePanel>
  </>
)}
```

where `itemPanel` is the existing `<ItemPanel …/>` element hoisted into a `const itemPanel = (<ItemPanel item={item} detail={sessionDetail} onRate={handleRate} ratingDisabled={!!pending || practicePaused} />);` just before `return`, keeping its comment. The citation `SidePanel` is rendered after the question one, so its `showModal()` runs later and it sits on top of the top layer.

`SessionShell.tsx`: add `<SkipLink />` as the first child of the outer `div` (import from `../layout/SkipLink`).

- [ ] **Step 4: Run the unit suite**

Run: `npm test`
Expected: PASS.

- [ ] **Step 5: Build, lint, commit**

Run: `npm run build && npm run lint` — Expected: exit 0.

```bash
git add frontend/src/components/lessons/QuestionCard.tsx frontend/src/pages/Session.tsx frontend/src/pages/Session.test.tsx frontend/src/components/lessons/SessionShell.tsx
git commit -m "feat(frontend): practice fits a phone — the question stays above the composer [S53]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 5: Navigation menu and skip link on every page

**Files:**
- Modify: `frontend/src/components/NavBar.tsx`, `frontend/src/components/PageShell.tsx`
- Test: `frontend/src/components/NavBar.test.tsx`, `frontend/src/components/PageShell.test.tsx`

**Interfaces:**
- Consumes: `SkipLink` (Task 3).

- [ ] **Step 1: Write the failing tests**

```tsx
// frontend/src/components/NavBar.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { NavBar } from "./NavBar";

vi.mock("../api/auth", () => ({
  useCurrentLearner: () => ({ data: { display_name: "Ada", handle: "ada", is_admin: false } }),
}));
vi.mock("../auth/session", () => ({ useSignOutEverywhere: () => async () => {} }));
vi.mock("./ThemeToggle", () => ({ ThemeToggle: () => null }));

describe("NavBar", () => {
  it("folds the links into a menu for narrow screens", () => {
    render(
      <MemoryRouter>
        <NavBar />
      </MemoryRouter>,
    );
    const menu = screen.getByRole("group", { name: "Menu" });
    expect(within(menu).getByRole("link", { name: "Chat" })).toHaveAttribute("href", "/app/chat");
    expect(within(menu).getByRole("link", { name: "Account" })).toBeInTheDocument();
  });
});
```

(`<details>` has the implicit role `group`; its accessible name comes from `aria-label`.)

```tsx
// frontend/src/components/PageShell.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { PageShell } from "./PageShell";

vi.mock("./NavBar", () => ({ NavBar: () => null }));
vi.mock("./ImpersonationBanner", () => ({ ImpersonationBanner: () => null }));

describe("PageShell", () => {
  it("lets a keyboard user skip straight to the page's content", () => {
    render(
      <MemoryRouter>
        <PageShell />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main");
    expect(screen.getByRole("main")).toHaveAttribute("id", "main");
  });
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `npm test -- src/components/NavBar.test.tsx src/components/PageShell.test.tsx`
Expected: FAIL — no group "Menu"; no skip link; `main` has no id.

- [ ] **Step 3: Implement**

`PageShell.tsx`: add `<SkipLink />` as the first child of the outer `div` and `id="main"` on `<main>`.

`NavBar.tsx`: compute the links once, `const links = [...LINKS, ...(learner?.is_admin ? [ADMIN_LINK] : [])];`, and a shared class function:

```tsx
const linkClass = ({ isActive }: { isActive: boolean }) =>
  `flex items-center gap-2 rounded-field px-3 py-2 text-caption transition-colors ${
    isActive
      ? "bg-primary/10 text-primary"
      : "text-base-content/70 hover:bg-base-200 hover:text-base-content"
  }`;
```

Change the inline `<nav className="flex items-center gap-1">` to `<nav aria-label="Main" className="hidden items-center gap-1 sm:flex">` mapping `links` with `className={linkClass}`, and add before it:

```tsx
{/* Below `sm` the links do not fit beside the logo and account controls (S53); a <details>
    disclosure opens and closes from the keyboard with no script of ours. */}
<details aria-label="Menu" className="dropdown sm:hidden">
  <summary className="btn btn-ghost btn-sm" aria-label="Open menu">
    <Menu size={18} />
  </summary>
  <ul className="dropdown-content menu bg-base-100 rounded-box border-base-300 z-50 mt-2 w-52 border p-2 shadow">
    {links.map(({ to, label, icon: Icon }) => (
      <li key={to}>
        <NavLink to={to} className={linkClass}>
          <Icon size={16} />
          {label}
        </NavLink>
      </li>
    ))}
  </ul>
</details>
```

(import `Menu` from `lucide-react`). Change the header's inner `px-6` to `px-4 sm:px-6`.

- [ ] **Step 4: Run the unit suite**

Run: `npm test`
Expected: PASS.

- [ ] **Step 5: Build, lint, commit**

Run: `npm run build && npm run lint` — Expected: exit 0.

```bash
git add frontend/src/components/NavBar.tsx frontend/src/components/NavBar.test.tsx frontend/src/components/PageShell.tsx frontend/src/components/PageShell.test.tsx
git commit -m "feat(frontend): the navigation folds into a menu on a phone, and every page has a skip link [S53]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 6: Named and modal controls — composer and delete confirmation

**Files:**
- Modify: `frontend/src/components/chat/Composer.tsx`, `frontend/src/components/removal/RemovalDialog.tsx`
- Test: `frontend/src/components/chat/Composer.test.tsx`, `frontend/src/components/removal/RemovalDialog.test.tsx` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `Composer.test.tsx` (reuse its render pattern):

```tsx
it("says which mode is on, and names the message box", () => {
  render(<Composer onSend={() => {}} disabled={false} />);
  expect(screen.getByRole("button", { name: "Chat" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "Agentic" })).toHaveAttribute("aria-pressed", "false");
  expect(screen.getByRole("textbox", { name: "Message" })).toBeInTheDocument();
});
```

Append to `RemovalDialog.test.tsx`:

```tsx
it("opens as a modal and closes on Escape", () => {
  const onClose = vi.fn();
  render(<RemovalDialog kind="source" id="s1" name="notes.pdf" open onClose={onClose} />);
  const dialog = screen.getByRole("dialog");
  expect(dialog).toHaveAttribute("data-modal", "true");
  fireEvent(dialog, new Event("cancel", { cancelable: true }));
  expect(onClose).toHaveBeenCalledTimes(1);
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `npm test -- src/components/chat/Composer.test.tsx src/components/removal/RemovalDialog.test.tsx`
Expected: FAIL — no `aria-pressed`, textbox unnamed, dialog not modal.

- [ ] **Step 3: Implement**

`Composer.tsx`: add `type="button" aria-pressed={mode === "chat"}` and `type="button" aria-pressed={mode === "agentic"}` to the two mode buttons; add `aria-label="Message"` to the `textarea`; add `type="button"` to Stop and Send.

`RemovalDialog.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";
// ...
  const dialogRef = useRef<HTMLDialogElement>(null);
  // Modal, as NewChatModal is (S53): focus stays inside, Escape cancels, and focus returns to
  // the delete button that opened it. `<dialog open>` gave none of the three.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    else if (!open && dialog.open) dialog.close();
  }, [open]);
```

and `<dialog ref={dialogRef} className="modal" aria-labelledby={`remove-${id}`} onCancel={(e) => { e.preventDefault(); onClose(); }}>` with `id={`remove-${id}`}` on the `<h3>`.

- [ ] **Step 4: Run the unit suite**

Run: `npm test`
Expected: PASS (the two existing `RemovalDialog` tests still find their content: the shim's `showModal` sets `open`).

- [ ] **Step 5: Build, lint, commit**

Run: `npm run build && npm run lint` — Expected: exit 0.

```bash
git add frontend/src/components/chat/Composer.tsx frontend/src/components/chat/Composer.test.tsx frontend/src/components/removal/RemovalDialog.tsx frontend/src/components/removal/RemovalDialog.test.tsx
git commit -m "fix(frontend): the composer's controls say what they are, and the delete confirmation is modal [S53]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 7: Announcements

**Files:**
- Create: `frontend/src/components/LiveAnnouncer.tsx`
- Modify: `frontend/src/components/chat/MessageList.tsx`, `frontend/src/pages/Chat.tsx`, `frontend/src/pages/Session.tsx`
- Test: `frontend/src/components/LiveAnnouncer.test.tsx`, `frontend/src/components/chat/MessageList.test.tsx` (extend)

**Interfaces:**
- Produces:
  - `LiveAnnouncer({ message }: { message: string })` — a persistent visually hidden `role="status"` (`aria-live="polite"`) region.
  - `useTurnAnnouncement(busy: boolean, error: string | null, grade?: CheckResult | null, notice?: string | null): string`.
  - `gradeAnnouncement(result: CheckResult): string` — `"Marked correct, scored 80%"` / `"Marked, not quite yet, scored 40%"`.

- [ ] **Step 1: Write the failing tests**

```tsx
// frontend/src/components/LiveAnnouncer.test.tsx
import { describe, expect, it } from "vitest";
import { render, renderHook, screen } from "@testing-library/react";
import { LiveAnnouncer, gradeAnnouncement, useTurnAnnouncement } from "./LiveAnnouncer";

const grade = { item_id: "i", score: 0.8, correct: true, components: [] };

describe("useTurnAnnouncement", () => {
  it("announces the start once, not each token", () => {
    const { result, rerender } = renderHook(
      ({ busy }) => useTurnAnnouncement(busy, null),
      { initialProps: { busy: false } },
    );
    expect(result.current).toBe("");
    rerender({ busy: true });
    expect(result.current).toBe("Guru is replying…");
    rerender({ busy: true });
    expect(result.current).toBe("Guru is replying…");
  });

  it("announces the end as finished, a grade, or the error", () => {
    const run = (error: string | null, g: typeof grade | null) => {
      const { result, rerender } = renderHook(
        ({ busy }) => useTurnAnnouncement(busy, error, g),
        { initialProps: { busy: true } },
      );
      rerender({ busy: false });
      return result.current;
    };
    expect(run(null, null)).toBe("Reply finished");
    expect(run(null, grade)).toBe("Marked correct, scored 80%");
    expect(run("The tutor is busy.", grade)).toBe("The tutor is busy.");
  });

  it("announces a practice notice when one appears", () => {
    const { result, rerender } = renderHook(
      ({ notice }) => useTurnAnnouncement(false, null, null, notice),
      { initialProps: { notice: null as string | null } },
    );
    rerender({ notice: "Practice paused" });
    expect(result.current).toBe("Practice paused");
  });
});

describe("gradeAnnouncement", () => {
  it("says a miss gently", () => {
    expect(gradeAnnouncement({ ...grade, correct: false, score: 0.4 })).toBe(
      "Marked, not quite yet, scored 40%",
    );
  });
});

describe("LiveAnnouncer", () => {
  it("is a polite status region", () => {
    render(<LiveAnnouncer message="Reply finished" />);
    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveTextContent("Reply finished");
  });
});
```

Append to `MessageList.test.tsx` (reuse its render helper and minimal props):

```tsx
it("is a log that does not read tokens aloud", () => {
  renderList(); // the file's existing helper; use its real name and props
  const log = screen.getByRole("log");
  expect(log).toHaveAttribute("aria-live", "off");
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `npm test -- src/components/LiveAnnouncer.test.tsx src/components/chat/MessageList.test.tsx`
Expected: FAIL — module not found; no `log` role.

- [ ] **Step 3: Implement**

```tsx
// frontend/src/components/LiveAnnouncer.tsx
import { useState } from "react";
import type { CheckResult } from "../api/sse";

export function gradeAnnouncement(result: CheckResult): string {
  const pct = Math.round(result.score * 100);
  return result.correct ? `Marked correct, scored ${pct}%` : `Marked, not quite yet, scored ${pct}%`;
}

/** What a screen reader is told about a turn (S53): that it started, and how it ended. Not the
 * reply itself — reading a streamed reply token by token is unusable, so the transcript is a
 * silent log the learner reads when told the reply is there.
 *
 * Derived during render from the previous value rather than set in an effect: an effect would
 * render once with the stale message first, and the lint rules here refuse state set in one. */
export function useTurnAnnouncement(
  busy: boolean,
  error: string | null,
  grade: CheckResult | null = null,
  notice: string | null = null,
): string {
  const [prev, setPrev] = useState({ busy, notice });
  const [message, setMessage] = useState("");
  if (busy !== prev.busy || notice !== prev.notice) {
    setPrev({ busy, notice });
    if (busy !== prev.busy) {
      setMessage(
        busy ? "Guru is replying…" : (error ?? (grade ? gradeAnnouncement(grade) : "Reply finished")),
      );
    } else if (notice) {
      setMessage(notice);
    }
  }
  return message;
}

/** One polite region per page, always mounted: a region inserted together with its text is
 * not reliably announced, one whose text changes is. */
export function LiveAnnouncer({ message }: { message: string }) {
  return (
    <div role="status" aria-live="polite" className="sr-only">
      {message}
    </div>
  );
}
```

`MessageList.tsx`: on the outer `div`, add `role="log" aria-live="off" aria-label="Conversation"`.

`Chat.tsx`: `const announcement = useTurnAnnouncement(!!pending, error);` and render `<LiveAnnouncer message={announcement} />` as the first child of `<main>`.

`Session.tsx`:

```tsx
const newest = messages.length ? messages[messages.length - 1] : null;
const grade = (newest?.check_result ?? null) as CheckResult | null;
const notice = endedNotice ?? (practicePaused ? "Practice paused" : null);
const announcement = useTurnAnnouncement(!!pending, error, grade, notice);
```

(import `CheckResult` from `../api/sse`), and `<LiveAnnouncer message={announcement} />` as the first child of `<main>`. The persisted transcript is refetched before `pending` clears (see `useChatConversation`), so at the moment `busy` turns false the newest message is this turn's.

- [ ] **Step 4: Run the unit suite**

Run: `npm test`
Expected: PASS. (The `Session.test.tsx` state must include `messages`; it already does.)

- [ ] **Step 5: Build, lint, commit**

Run: `npm run build && npm run lint` — Expected: exit 0. If `react-hooks` flags the render-time `setState`, that is the documented "adjusting state when a prop changes" pattern; check the exact rule name in the error before changing approach.

```bash
git add frontend/src/components/LiveAnnouncer.tsx frontend/src/components/LiveAnnouncer.test.tsx frontend/src/components/chat/MessageList.tsx frontend/src/components/chat/MessageList.test.tsx frontend/src/pages/Chat.tsx frontend/src/pages/Session.tsx
git commit -m "feat(frontend): a screen reader hears that a reply started, how it ended, and the grade [S53]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 8: axe and the keyboard and phone journeys

**Files:**
- Modify: `frontend/package.json` (`@axe-core/playwright` dev dependency), `frontend/playwright.config.ts`
- Create: `frontend/e2e/a11y.ts`, `frontend/e2e/a11y.spec.ts`
- Modify: `frontend/e2e/chat.spec.ts`, `practice.spec.ts`, `curriculum.spec.ts`, `library.spec.ts`, `admin.spec.ts` (one `expectAccessible` call each)

**Interfaces:**
- Produces: `expectAccessible(page: Page): Promise<void>`.

- [ ] **Step 1: Install and add the helper and projects**

Run: `npm install --save-dev @axe-core/playwright@^4.13.0`

```ts
// frontend/e2e/a11y.ts
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

/** Fails on any serious or critical WCAG 2.1 A/AA violation on the page as it stands (S53).
 *
 * Moderate and minor findings are not gated: axe reports some of them on markup that is
 * correct in context, and a gate people learn to ignore is worse than none. The message lists
 * each rule and the elements it fired on, which is what a fix needs. */
export async function expectAccessible(page: Page): Promise<void> {
  const { violations } = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const blocking = violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  const report = blocking
    .map((v) => `${v.id} (${v.impact}): ${v.help}\n  ${v.nodes.map((n) => n.target.join(" ")).join("\n  ")}`)
    .join("\n");
  expect(blocking, report).toEqual([]);
}
```

`playwright.config.ts`: replace `projects` with

```ts
projects: [
  {
    name: "desktop",
    use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
  },
  {
    // Only the accessibility journeys run twice: everything else is about the product behind
    // the layout, and running it at two widths would double the suite to learn nothing.
    name: "phone",
    testMatch: /a11y\.spec\.ts/,
    use: { ...devices["Desktop Chrome"], viewport: { width: 390, height: 844 }, hasTouch: true },
  },
],
```

(Chromium at phone size rather than an emulated iPhone: WebKit is not installed for these journeys.)

- [ ] **Step 2: Write the journeys**

```ts
// frontend/e2e/a11y.spec.ts
import { expect, test, type Page } from "@playwright/test";

import { expectAccessible } from "./a11y";
import { seedSubjectWithPlan, signIn } from "./journey";

/** Keyboard and phone-width use of the screens a learner spends their time on (S53). Runs under
 * both the desktop and the phone project; each test says which width it is about. */

async function newGeneralChat(page: Page): Promise<void> {
  await page.goto("/app/chat");
  if (test.info().project.name === "phone") {
    await page.getByRole("button", { name: "Conversations" }).click();
  }
  await page.getByRole("button", { name: "New chat" }).click();
  await page.getByRole("button", { name: /General — no library grounding/ }).click();
  await page.getByRole("button", { name: "Start chat" }).click();
  await expect(page).toHaveURL(/\/app\/chat\/[0-9a-f-]{36}/);
}

async function noSidewaysScroll(page: Page): Promise<void> {
  const [scroll, inner] = await page.evaluate(() => [
    document.documentElement.scrollWidth,
    window.innerWidth,
  ]);
  expect(scroll).toBeLessThanOrEqual(inner);
}

test("chat can be used from the keyboard alone", async ({ page }) => {
  await signIn(page);
  await newGeneralChat(page);
  await expectAccessible(page);

  // The skip link is the first stop, and it lands in the content.
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Enter");

  const box = page.getByRole("textbox", { name: "Message" });
  await box.focus();
  await page.keyboard.type("What is a derivative?");
  const stream = page.waitForResponse(
    (r) => r.url().includes("/messages") && r.request().method() === "POST",
  );
  await page.keyboard.press("Enter");
  await (await stream).finished();
  await expect(page.getByRole("status")).toHaveText(/Reply finished|Marked/);
  await expectAccessible(page);
});

test("on a phone the conversation list is a drawer and nothing scrolls sideways", async ({
  page,
}) => {
  test.skip(test.info().project.name !== "phone", "phone layout");
  await signIn(page);
  await newGeneralChat(page);
  // Starting a chat navigated, which closes the drawer.
  await expect(page.getByRole("dialog", { name: "Conversations" })).toBeHidden();
  await noSidewaysScroll(page);

  await page.getByRole("button", { name: "Conversations" }).click();
  const drawer = page.getByRole("dialog", { name: "Conversations" });
  await expect(drawer).toBeVisible();
  await expectAccessible(page);
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(page.getByRole("button", { name: "Conversations" })).toBeFocused();
});

test("on a phone the practice question stays in view and opens in full", async ({ page }) => {
  test.skip(test.info().project.name !== "phone", "phone layout");
  await signIn(page);
  const subjectId = await seedSubjectWithPlan(page, `Understand eigenvalues ${Date.now()}`);
  await page.goto(`/app/lessons?subject_id=${subjectId}`);
  await page.getByRole("button", { name: "Start practice" }).click();
  await expect(page).toHaveURL(/\/app\/lessons\/session\/[0-9a-f-]{36}/);

  await expect(page.getByText("Preparing your practice…")).toHaveCount(0);
  await page.getByRole("button", { name: "Show question" }).click();
  const sheet = page.getByRole("dialog", { name: "Practice question" });
  await expect(sheet).toBeVisible();
  await expect(sheet.getByText("Practice item")).toBeVisible();
  await expectAccessible(page);
  await sheet.getByRole("button", { name: "Close Practice question" }).click();
  await noSidewaysScroll(page);
});

test("on a phone the navigation is a menu", async ({ page }) => {
  test.skip(test.info().project.name !== "phone", "phone layout");
  await signIn(page);
  await page.goto("/app/dashboard");
  await page.getByRole("button", { name: "Open menu" }).click();
  await page.getByRole("link", { name: "Notes" }).click();
  await expect(page).toHaveURL(/\/app\/notes/);
  await noSidewaysScroll(page);
  await expectAccessible(page);
});
```

Spec deviation, recorded here so it is a ruling and not a surprise: the spec's keyboard journey opens a citation marker. No offline journey produces a citation (the `shaped` provider cites nothing and the library journey does not chat), so citation focus is pinned by the `CitationPane` and `SidePanel` unit tests instead.

In each existing spec, import `expectAccessible` from `./a11y` and call it once where the main screen has settled:
- `chat.spec.ts`, "signing in, asking, and finding the answer…": after the reload's two `toBeVisible` assertions.
- `practice.spec.ts`, "a practice session asks a question…": after the panel's placeholder is gone.
- `curriculum.spec.ts`, first test: once the wizard's first step is visible.
- `library.spec.ts`, first test: once the source is listed as ready.
- `admin.spec.ts`, "an administrator reads…": once the portal has rendered.

- [ ] **Step 3: Run the journeys and fix what axe reports**

Run (Docker Postgres and Redis up, as for the existing journeys): `npm run e2e`
Expected on the first run: the new tests run; axe may report violations on pages this plan did not touch. For each serious/critical one, fix it at its source (missing label, contrast token, landmark) and add a unit test where the fix is a component's markup. Re-run until green. Record each fix in the ledger with the rule id.

- [ ] **Step 4: Unit suite, build, lint**

Run: `npm test && npm run build && npm run lint`
Expected: all exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/playwright.config.ts frontend/e2e/a11y.ts frontend/e2e/a11y.spec.ts frontend/e2e/chat.spec.ts frontend/e2e/practice.spec.ts frontend/e2e/curriculum.spec.ts frontend/e2e/library.spec.ts frontend/e2e/admin.spec.ts
# plus each file an axe fix touched, by name
git commit -m "test(e2e): axe on every main screen, and keyboard and phone journeys [S53]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 9: Docs and the full gate

**Files:**
- Modify: `docs/RUNBOOK.md` (new section), `docs/guru-suggestions-tracker.md` (S53 row, counts, next-up), `CLAUDE.md` (one line under Key Technical Decisions)

- [ ] **Step 1: RUNBOOK section**

Append a section after the last numbered one (read the file for the next number):

```markdown
## 21. Accessibility gate (S53)

- `npm run e2e` runs every browser journey on a 1280×800 desktop and runs `e2e/a11y.spec.ts`
  again at 390×844. Each main screen is scanned by axe (`e2e/a11y.ts`) for WCAG 2.1 A/AA.
- A failure lists `rule-id (impact): help` and the selectors it fired on. Look the rule up at
  `https://dequeuniversity.com/rules/axe/<version>/<rule-id>`, fix the markup, and add a unit
  test when the fix is in a component.
- Only `serious` and `critical` fail the run. Run a manual screen-reader pass (VoiceOver on
  macOS: ⌘F5) over chat and practice before a release; the scan cannot hear what is announced.
- Layout has one breakpoint, `WIDE_QUERY` (1024 px) in `src/hooks/useMediaQuery.ts`. Below it
  side panels are modal sheets (`src/components/layout/SidePanel.tsx`).
```

- [ ] **Step 2: Tracker and CLAUDE.md**

- Tracker S53 row: status `Completed`; summary "Parser-located code before LaTeX rewriting; side panels as sheets below 1024 px; skip link, labelled and modal controls, turn announcements; axe on the main screens at desktop and phone width." Links: rich text, `SidePanel`, `e2e/a11y.spec.ts`. Update the status counts and the "Next up" line to "S62 part B". Add a "Deferred minors" entry for anything ledgered as deferred, and "Manual screen-reader pass before launch".
- CLAUDE.md, Key Technical Decisions, one bullet: "**Accessible at phone width** (S53) — one breakpoint (`WIDE_QUERY`, 1024 px); below it side panels are modal sheets (`SidePanel`), so focus, Escape and the backdrop come from the browser. Every screen a journey reaches is scanned by axe; serious and critical violations fail `npm run e2e`. See [docs/RUNBOOK.md](docs/RUNBOOK.md) §21."

- [ ] **Step 3: Full gates**

Run (from repo root): `uv run poe check` (background; expected unchanged — no backend change) and from `frontend/`: `npm run build && npm test && npm run lint && npm run e2e`.
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add docs/RUNBOOK.md docs/guru-suggestions-tracker.md CLAUDE.md
git commit -m "docs: S53 accessibility gate, tracker and decisions [S53]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```
