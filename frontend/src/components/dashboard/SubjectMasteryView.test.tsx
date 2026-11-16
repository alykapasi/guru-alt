import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { SubjectMasteryView } from "./SubjectMasteryView";

/** A topic nobody has answered on says so once. Repeating it on the topic, on a coverage line
 * and on every component told the learner nothing new three times over. */

const kc = (name: string, assessed: boolean) => ({
  kc_id: name,
  kc_name: name,
  assessed,
  ability: 0.4,
  uncertainty: 0.3,
  mastered: false,
  evidence: null,
});

const mastery = vi.hoisted(() => ({ data: null as unknown }));
vi.mock("../../api/hooks", () => ({ useSubjectMastery: () => ({ data: mastery.data }) }));
vi.mock("./MasteryEvidence", () => ({ MasteryEvidence: () => null }));

function topic(name: string, kcs: ReturnType<typeof kc>[]) {
  const assessed = kcs.filter((k) => k.assessed).length;
  return {
    topic_id: name,
    topic_name: name,
    assessed_kcs: assessed,
    total_kcs: kcs.length,
    ability: 0.4,
    uncertainty: 0.3,
    kcs,
  };
}

function renderView(topics: ReturnType<typeof topic>[]) {
  mastery.data = {
    ability: 0.4,
    uncertainty: 0.3,
    assessed_kcs: topics.reduce((n, t) => n + t.assessed_kcs, 0),
    total_kcs: topics.reduce((n, t) => n + t.total_kcs, 0),
    topics,
  };
  return render(<SubjectMasteryView subjectId="s-1" />);
}

describe("mastery by topic", () => {
  it("says a topic is not assessed once, not on every line under it", () => {
    renderView([
      topic("Foundations", [kc("Core vocabulary", false), kc("First principles", false)]),
    ]);
    const row = screen.getByRole("group", { name: "Foundations" });
    expect(within(row).getAllByText(/not assessed/i)).toHaveLength(1);
    expect(within(row).getByText("Core vocabulary")).toBeInTheDocument();
  });

  it("still shows how much of a partly assessed topic has evidence", () => {
    renderView([
      topic("Foundations", [kc("Core vocabulary", true), kc("First principles", false)]),
    ]);
    const row = screen.getByRole("group", { name: "Foundations" });
    expect(within(row).getByText("1 of 2 assessed")).toBeInTheDocument();
  });
});
