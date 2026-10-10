import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

/** Fails on any serious or critical WCAG 2.1 A/AA violation on the page as it stands (S53).
 *
 * Moderate and minor findings are not gated: axe reports some of them on markup that is
 * correct in context, and a gate people learn to ignore is worse than none. The message lists
 * each rule and the elements it fired on, which is what a fix needs. */
export async function expectAccessible(page: Page): Promise<void> {
  // A dialog fading in or out is half-transparent, and axe measures the contrast of whatever
  // frame it lands on — so a scan taken mid-transition reports text that is fine at rest.
  await page.waitForFunction(() =>
    document.getAnimations().every((a) => a.playState !== "running"),
  );
  const { violations } = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const blocking = violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  const report = blocking
    .map(
      (v) =>
        `${v.id} (${v.impact}): ${v.help}\n  ${v.nodes.map((n) => `${n.target.join(" ")} — ${n.failureSummary?.split("\n").at(-1)?.trim() ?? ""}`).join("\n  ")}`,
    )
    .join("\n");
  expect(blocking, report).toEqual([]);
}
