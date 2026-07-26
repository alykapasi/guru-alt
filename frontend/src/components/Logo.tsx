/** The brand mark: 🌱 is the *only* emoji used in the UI (growth, tied to durable-learning
 * mastery, harmonizes with the teal primary) — every functional icon elsewhere uses lucide-react. */
export function Logo() {
  return (
    <span className="flex items-center gap-2">
      <span className="text-xl" aria-hidden="true">
        🌱
      </span>
      <span className="text-h3 text-base-content">Guru</span>
    </span>
  );
}
