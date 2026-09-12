import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MessageList } from "./MessageList";
import type { components } from "../../api/schema";

type Message = components["schemas"]["MessageRead"];

/** The transcript is a bounded page now (S62), so the reader needs a way back through it —
 * and reaching for it must not cost them their place. */

function message(id: string, content: string): Message {
  return {
    id,
    role: "user",
    content,
    citations: null,
    check_result: null,
    created_at: "2026-01-01T00:00:00Z",
  } as unknown as Message;
}

function renderList(props: Partial<Parameters<typeof MessageList>[0]> = {}) {
  return render(
    <MessageList
      messages={[message("m1", "first"), message("m2", "second")]}
      pending={null}
      goal={null}
      awaitingGoalAccept={false}
      onAcceptGoal={() => {}}
      onCitationClick={() => {}}
      {...props}
    />,
  );
}

describe("MessageList", () => {
  it("offers no way back when the page is the whole conversation", () => {
    renderList({ hasEarlier: false });
    expect(screen.queryByRole("button", { name: /load earlier/i })).toBeNull();
  });

  it("offers to load earlier messages when older ones exist", () => {
    renderList({ hasEarlier: true });
    expect(screen.getByRole("button", { name: /load earlier messages/i })).toBeTruthy();
  });

  it("asks for the older page when the control is used", async () => {
    const onLoadEarlier = vi.fn();
    renderList({ hasEarlier: true, onLoadEarlier });

    await userEvent.click(screen.getByRole("button", { name: /load earlier messages/i }));

    expect(onLoadEarlier).toHaveBeenCalledTimes(1);
  });

  it("does not let the reader ask twice while the page is in flight", () => {
    renderList({ hasEarlier: true, isLoadingEarlier: true });
    const button = screen.getByRole("button", { name: /loading/i }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  it("still renders the messages it was given", () => {
    renderList({ hasEarlier: true });
    expect(screen.getByText("first")).toBeTruthy();
    expect(screen.getByText("second")).toBeTruthy();
  });
});
