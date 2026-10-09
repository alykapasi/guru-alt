import { useState } from "react";
import type { CheckResult } from "../api/sse";

export function gradeAnnouncement(result: CheckResult): string {
  const pct = Math.round(result.score * 100);
  return result.correct
    ? `Marked correct, scored ${pct}%`
    : `Marked, not quite yet, scored ${pct}%`;
}

/** What a screen reader is told about a turn (S53): that it started, and how it ended. Not the
 * reply itself — reading a streamed reply token by token is unusable, so the transcript is a
 * silent log the learner reads when told the reply is there.
 *
 * Derived during render from the previous value rather than set in an effect: an effect would
 * render once with the stale message first. */
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
        busy
          ? "Guru is replying…"
          : (error ?? (grade ? gradeAnnouncement(grade) : "Reply finished")),
      );
    } else if (notice) {
      setMessage(notice);
    }
  }
  return message;
}
