import { expect, test, type Page } from "@playwright/test";

import { expectAccessible } from "./a11y";
import { seedSubjectWithPlan, signIn } from "./journey";

/** Keyboard and phone-width use of the screens a learner spends their time on (S53). Runs under
 * both the desktop and the phone project; a test about one width says so and skips the other.
 *
 * A citation is not opened here: nothing offline produces one (the stand-in provider cites
 * nothing), so citation focus is pinned by the CitationPane and SidePanel unit tests. */

const phone = () => test.info().project.name === "phone";

async function newGeneralChat(page: Page): Promise<void> {
  await page.goto("/app/chat");
  if (phone()) await page.getByRole("button", { name: "Conversations" }).click();
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

  // The skip link is the first stop on a fresh page, and it lands in the content. Fresh, because
  // closing the drawer rightly handed focus back to the button that opened it.
  await page.reload();
  // The shell renders once the session check answers; a Tab pressed before then goes nowhere.
  // Wait for the transcript too: pinning it to the newest message once moved where Tab starts,
  // and the first Tab skipped the link.
  await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible();
  await expect(page.getByRole("log", { name: "Conversation" })).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/#main$/);

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
  test.skip(!phone(), "phone layout");
  await signIn(page);
  await newGeneralChat(page);
  // Starting a chat navigated, which closes the drawer.
  await expect(page.getByRole("dialog", { name: "Conversations" })).toBeHidden();
  await noSidewaysScroll(page);

  const opener = page.getByRole("button", { name: "Conversations" });
  await opener.click();
  const drawer = page.getByRole("dialog", { name: "Conversations" });
  await expect(drawer).toBeVisible();
  await expectAccessible(page);
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(opener).toBeFocused();
});

test("on a phone the practice question stays in view and opens in full", async ({ page }) => {
  test.skip(!phone(), "phone layout");
  await signIn(page);
  const subjectId = await seedSubjectWithPlan(page, `Understand eigenvalues ${Date.now()}`);
  await page.goto(`/app/lessons?subject_id=${subjectId}`);
  await page.getByRole("button", { name: "Start practice" }).click();
  await expect(page).toHaveURL(/\/app\/lessons\/session\/[0-9a-f-]{36}/);

  await expect(page.getByText(/In your own words, explain/).first()).toBeVisible();
  await page.getByRole("button", { name: "Show question" }).click();
  const sheet = page.getByRole("dialog", { name: "Practice question" });
  await expect(sheet).toBeVisible();
  await expect(sheet.getByText("Practice item")).toBeVisible();
  await expectAccessible(page);
  await sheet.getByRole("button", { name: "Close Practice question" }).click();
  await expect(sheet).toBeHidden();
  await noSidewaysScroll(page);
});

test("on a phone the navigation is a menu", async ({ page }) => {
  test.skip(!phone(), "phone layout");
  await signIn(page);
  await page.goto("/app/dashboard");
  // A <summary> is a disclosure control, not a button, to the accessibility tree.
  await page.getByRole("group", { name: "Menu" }).locator("summary").click();
  await page.getByRole("link", { name: "Notes" }).click();
  await expect(page).toHaveURL(/\/app\/notes/);
  await noSidewaysScroll(page);
  await expectAccessible(page);
});
