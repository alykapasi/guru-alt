import { expect, test, type Page } from "@playwright/test";

/** Mirrors `TERMINAL_EVENTS` in `src/api/sse.ts`. Restated rather than imported because that
 * module reaches `import.meta.env`, which the Playwright runner has no equivalent of — and a
 * spec that cannot load is worse than a duplicated four-element list. Adding a terminal event
 * type to the product without adding it here fails this test rather than weakening it. */
const TERMINAL_EVENTS = ["done", "awaiting_reply", "committed", "error"];

/** The first browser journey (S58): register, chat, and come back to find it there.
 *
 * Everything this covers is invisible to both suites on either side of it. The backend tests
 * drive `run_tutor_turn` directly and never serialise an SSE frame; the component tests render
 * the chat with a mocked client and never make a request. The seam between them — the event
 * stream, the credentialed cross-origin fetch, the cookie the browser decides whether to send —
 * is where a change breaks the product while both suites stay green.
 *
 * The account is registered through the form rather than through a development sign-in, for
 * two reasons: the dev-login button is compiled out of a production build, which this runs
 * against on purpose, and registering is the path a first user actually takes. */

/** A fresh account per run. The journey commits, and a fixed address would make the second
 * run of the day fail on a unique constraint with a message about email addresses. */
function newAccount() {
  const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  // example.com, not example.test: the API validates deliverability, and the reserved
  // special-use TLDs are refused before the address ever reaches a handler.
  return { email: `journey-${stamp}@example.com`, password: "journey-password-1" };
}

async function register(page: Page): Promise<void> {
  const account = newAccount();
  await page.goto("/signin");
  await page.getByRole("button", { name: "Create one" }).click();
  await page.getByLabel("Email").fill(account.email);
  // Not an exact match: in register mode the field's label carries the "At least 12
  // characters" hint, so its accessible name is the two of them together.
  await page.getByLabel(/^Password/).fill(account.password);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/app\/chat/);
}

async function startGeneralChat(page: Page): Promise<void> {
  await page.getByRole("button", { name: "New chat" }).click();
  await page.getByRole("button", { name: /General — no library grounding/ }).click();
  await page.getByRole("button", { name: "Start chat" }).click();
  await expect(page).toHaveURL(/\/app\/chat\/[0-9a-f-]{36}/);
}

test("a signed-out browser is sent to sign in rather than a shell of failed calls", async ({
  page,
}) => {
  await page.goto("/app/chat");
  await expect(page).toHaveURL(/\/signin/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("registering, asking, and finding the answer still there after a reload", async ({ page }) => {
  await register(page);
  await startGeneralChat(page);

  const stream = page.waitForResponse(
    (r) => r.url().includes("/messages") && r.request().method() === "POST",
  );
  await page.getByPlaceholder("Message Guru").fill("What is a derivative?");
  await page.getByRole("button", { name: "Send" }).click();

  // The learner's own message appears immediately; the reply arrives over the event stream.
  await expect(page.getByText("What is a derivative?")).toBeVisible();
  await expect(page.getByText("Hello from the fake tutor.")).toBeVisible();

  // Waiting for the stream to *finish* is not impatience, and getting this wrong made this
  // test fail about one run in five. The reply visible above is the live buffer, and the
  // assistant message is committed only after the last frame — so a reload taken while the
  // stream is open correctly finds no assistant message, because an interrupted reply is
  // deliberately discarded rather than persisted half-written. The flake was the journey
  // racing that commit, not the product losing a turn.
  await (await stream).finished();

  // The turn is only real if it survives the page. Until the tenth pass the evidence of a
  // turn lived in the stream and died with it, so this is the assertion that would have
  // caught it: same conversation, fresh document, both messages still there.
  const conversation = page.url();
  await page.reload();
  await expect(page).toHaveURL(conversation);
  await expect(page.getByText("What is a derivative?")).toBeVisible();
  await expect(page.getByText("Hello from the fake tutor.")).toBeVisible();
});

test("the reply is streamed into the page, not delivered in one piece", async ({ page }) => {
  await register(page);
  await startGeneralChat(page);

  // Watch the response frames rather than the DOM: a rendered reply looks identical whether it
  // arrived as five events or as one, so "streaming" degrading to a single blocking response
  // is invisible on screen. What this pins is the wire the *browser* was served — many frames
  // over text/event-stream, through the real cross-origin credentialed request — not that the
  // DOM painted them one at a time, which the component test covers.
  const stream = page.waitForResponse(
    (r) => r.url().includes("/messages") && r.request().method() === "POST",
  );
  await page.getByPlaceholder("Message Guru").fill("Explain streaming.");
  await page.getByRole("button", { name: "Send" }).click();

  const response = await stream;
  expect(response.headers()["content-type"]).toContain("text/event-stream");

  // Parsed rather than matched as a substring: the first version of this looked for
  // `"type":"token"` and found nothing, because `json.dumps` puts a space after the colon.
  // It reported zero tokens for a stream that was working perfectly.
  const events = (await response.text())
    .split("\n")
    .filter((line) => line.startsWith("data: "))
    .map((line) => JSON.parse(line.slice("data: ".length)) as { type: string });

  expect(events.filter((e) => e.type === "token").length).toBeGreaterThan(1);
  // A stream that simply runs out is how a half-written explanation was once left on screen
  // looking finished, so the client treats the absence of a terminal frame as a failure. The
  // terminal frame here is `awaiting_reply` rather than `done`: a new conversation's first
  // turn goes through the refinement gate, which proposes a goal and waits for an answer.
  expect(TERMINAL_EVENTS).toContain(events.at(-1)?.type);
  // Exactly one, not merely visible — and this is the assertion CI corrected.
  //
  // `toBeVisible` does not retry past a strict-mode violation, so when the transcript briefly
  // held both the live buffer and the persisted message, it failed on the first poll rather
  // than waiting for the duplicate to resolve. That is how a real defect surfaced: the pending
  // buffer was cleared only after three refetches, two of which change nothing the transcript
  // renders, so on a slow connection a learner would read their reply twice.
  //
  // Stated plainly, because it matters for how much this line is worth: the duplicate has
  // never been reproduced locally — the window is too short on this machine, with or without
  // the fix — so CI is the only place it has ever been observed, and the only place the fix
  // has been checked.
  await expect(page.getByText("Hello from the fake tutor.")).toHaveCount(1);
});
