import { describe, expect, it } from "vitest";
import { practiceOver } from "./useChatConversation";

/** A practice round that ended must still read as ended after a reload. The server records
 * only "chatting" once an answer is graded or skipped, so a started session in that phase is
 * over — the page used to keep the outcome in memory alone, and a reload reopened the box. */

describe("practiceOver", () => {
  const session = (phase: string) => ({ kind: "session", phase });

  it("is over once a started session has nothing in play", () => {
    expect(practiceOver(session("chatting"), 2, false)).toBe(true);
  });

  it("is not over while a question waits or practice is paused", () => {
    expect(practiceOver(session("awaiting_answer"), 2, false)).toBe(false);
    expect(practiceOver(session("practice_paused"), 2, false)).toBe(false);
  });

  it("is not over before the first turn or while one is running", () => {
    expect(practiceOver(session("chatting"), 0, false)).toBe(false);
    expect(practiceOver(session("chatting"), 2, true)).toBe(false);
  });

  it("never applies to an ordinary chat", () => {
    expect(practiceOver({ kind: "chat", phase: "chatting" }, 2, false)).toBe(false);
  });
});
