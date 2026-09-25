/** FSRS's four grades, in the order the learner sees them. The numbers are the contract with
 * `grade_flashcard` (1=Again … 4=Easy) — see app/learning/grading.py's _RATING_SCORE.
 *
 * Lives here rather than inside FlashcardPanel.tsx so it has exactly one copy: FlashcardPanel's
 * buttons and the session view's turn-content label (Session.tsx) both read it, and a component
 * file may only export the component itself (react-refresh's rule) — exporting it from there
 * traded a lint warning for a second hand-maintained mapping, which is worse. */
export const RATINGS: { label: string; value: number }[] = [
  { label: "Again", value: 1 },
  { label: "Hard", value: 2 },
  { label: "Good", value: 3 },
  { label: "Easy", value: 4 },
];
