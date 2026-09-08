/** Turning the estimator's numbers into something a dashboard can honestly show.
 *
 * Ability is logit-scale (TECHNICAL_DESIGN §7.3's `E = sigmoid(θ - d)`), so the sigmoid of it
 * is the learner's **expected score on a question of average difficulty** — not the share of a
 * subject they understand. The two were being displayed as the same thing, and the gap is
 * widest exactly where it matters: an unassessed component sits at the prior, ability 0, which
 * reads as a confident-looking "50%" derived from no evidence at all.
 */
export function expectedScorePercent(ability: number): number {
  return Math.round((100 / (1 + Math.exp(-ability))) * 10) / 10;
}

/** v1-arbitrary bands on uncertainty, matching TECHNICAL_DESIGN §7.4's own target UX language
 * ("Calculus 62% (wide)") — not calibrated against real outcome data. */
export function masteryQualifier(uncertainty: number): string {
  if (uncertainty >= 0.7) return "wide";
  if (uncertainty >= 0.4) return "developing";
  return "confident";
}

/** How much of a subject or topic has been assessed at all. */
export function coverageLabel(assessed: number, total: number): string {
  if (total === 0) return "nothing to assess yet";
  if (assessed === 0) return "not assessed yet";
  return `${assessed} of ${total} assessed`;
}
