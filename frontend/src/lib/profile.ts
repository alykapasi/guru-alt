/** Turns a `snake_case` dimension key into a readable label — "optimal_challenge" -> "Optimal
 * challenge". */
export function humanizeKey(key: string): string {
  const spaced = key.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** Learner-profile dimension values are per-dimension shaped (float, string, list, or a nested
 * object — see app/learning/profile_estimators.py::DIMENSION_SPECS) with no single schema, so
 * this renders any of them generically rather than needing 12 bespoke formatters. */
export function formatDimensionValue(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "number") return String(Math.round(value * 100) / 100);
  if (typeof value === "string") return humanizeKey(value);
  if (Array.isArray(value)) return value.map(formatDimensionValue).join(", ");
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([k, v]) => `${humanizeKey(k)}: ${formatDimensionValue(v)}`)
      .join(" · ");
  }
  return String(value);
}
