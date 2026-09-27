/** Learner-facing words for preference values (S02). */
export const OPTION_LABELS: Record<string, string> = {
  auto: "Adapt to me",
  guided: "Guided",
  exploration: "Exploration",
  introductory: "Introductory",
  standard: "Standard",
  advanced: "Advanced",
  outline: "Outline",
  narrative: "Narrative",
  mnemonic: "Mnemonic",
  worked_examples: "Worked examples",
  fewer: "Fewer hints",
  some: "Some hints",
  more: "More hints",
  brisk: "Brisk",
  unhurried: "Unhurried",
};

export const optionLabel = (value: string) => OPTION_LABELS[value] ?? value;

/** Profile dimensions a setting overrides while it is pinned. */
export const OVERRIDDEN_DIMENSIONS: Record<string, string> = {
  help_seeking: "hints",
  persistence: "hints",
  pace: "pace",
  note_format: "note_format",
};

/** What each guidance mode means (V07/S11), carried over from the old guidance toggle. */
export const VALUE_CAPTIONS: Record<string, string> = {
  guided: "Guru can add a detour on its own when you are stuck.",
  exploration: "Guru offers detours; you decide.",
};
