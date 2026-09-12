import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LessonStepRow } from "./LessonStepRow";
import type { components } from "../../api/schema";

type LessonStep = components["schemas"]["LessonStepRead"];

/** A detour reorders the plan to put a prerequisite first. A reordering nobody explains is
 * indistinguishable from the plan changing its mind, which is the thing a learner would
 * reasonably lose trust over — so these hold that the explanation actually appears (S11). */

const KC_NAMES: Record<string, string> = {
  "kc-projection": "Projections",
  "kc-least-squares": "Least squares",
};

function stubKcLookup() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input instanceof Request ? input.url : input);
      const id = url.split("/kcs/")[1] ?? "";
      return new Response(JSON.stringify({ id, name: KC_NAMES[id] ?? id }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
}

function step(overrides: Partial<LessonStep> = {}): LessonStep {
  return {
    kc_id: "kc-least-squares",
    order: 0,
    step_type: "new",
    status: "active",
    target_difficulty: null,
    hint_density: null,
    preferred_item_type: null,
    detour_for: null,
    detour_reason: null,
    ...overrides,
  } as LessonStep;
}

function renderRow(s: LessonStep) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <LessonStepRow step={s} />
    </QueryClientProvider>,
  );
}

describe("a lesson plan row", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("labels an ordinary step as new", () => {
    stubKcLookup();
    renderRow(step());
    expect(screen.getByText("New")).toBeInTheDocument();
    expect(screen.queryByText("Detour")).not.toBeInTheDocument();
  });

  it("labels a review as a review", () => {
    stubKcLookup();
    renderRow(step({ step_type: "review" }));
    expect(screen.getByText("Review")).toBeInTheDocument();
  });

  it("calls a detour a detour rather than showing it as new work", () => {
    stubKcLookup();
    renderRow(
      step({
        kc_id: "kc-projection",
        step_type: "detour",
        detour_for: "kc-least-squares",
        detour_reason: "Projections came up as the blocker.",
      }),
    );
    expect(screen.getByText("Detour")).toBeInTheDocument();
    expect(screen.queryByText("New")).not.toBeInTheDocument();
  });

  it("says what the detour is clearing the way back to, and why", async () => {
    stubKcLookup();
    renderRow(
      step({
        kc_id: "kc-projection",
        step_type: "detour",
        detour_for: "kc-least-squares",
        detour_reason: "Projections came up as the blocker.",
      }),
    );
    expect(
      await screen.findByText(/Clearing the way back to Least squares. Projections came up/),
    ).toBeInTheDocument();
  });

  it("still explains itself when the component it is for cannot be named", () => {
    // The KC lookup can fail or be slow; a detour with no explanation at all is the state
    // this item exists to remove, so the fallback still says what the step is doing.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 500 })),
    );
    renderRow(
      step({ step_type: "detour", detour_for: "kc-least-squares", detour_reason: "Blocked." }),
    );
    expect(screen.getByText(/Clearing the way. Blocked./)).toBeInTheDocument();
  });
});
