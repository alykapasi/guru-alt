/** Ability is logit-scale (TECHNICAL_DESIGN §7.3's `E = sigmoid(θ - d)`) — sigmoid maps it to
 * the 0-100% a learner actually reads on a dashboard. */
export function masteryPercent(ability: number): number {
  return Math.round((100 / (1 + Math.exp(-ability))) * 10) / 10;
}

/** v1-arbitrary bands on uncertainty, matching TECHNICAL_DESIGN §7.4's own target UX language
 * ("Calculus 62% (wide)") — not calibrated against real outcome data. */
export function masteryQualifier(uncertainty: number): string {
  if (uncertainty >= 0.7) return "wide";
  if (uncertainty >= 0.4) return "developing";
  return "confident";
}
