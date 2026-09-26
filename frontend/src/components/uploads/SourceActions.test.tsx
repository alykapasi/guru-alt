import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SourceActions } from "./SourceActions";

describe("SourceActions", () => {
  it("retries a failed source straight away", async () => {
    const onRetry = vi.fn();
    render(<SourceActions status="failed" onRetry={onRetry} />);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledWith(false);
  });

  it("asks before re-processing a finished source", async () => {
    const onRetry = vi.fn();
    render(<SourceActions status="done" onRetry={onRetry} />);
    await userEvent.click(screen.getByRole("button", { name: "Re-process" }));
    expect(onRetry).not.toHaveBeenCalled();
    expect(screen.getByText(/already in your library/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Replace passages" }));
    expect(onRetry).toHaveBeenCalledWith(true);
  });

  it("lets the learner back out", async () => {
    const onRetry = vi.fn();
    render(<SourceActions status="done" onRetry={onRetry} />);
    await userEvent.click(screen.getByRole("button", { name: "Re-process" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onRetry).not.toHaveBeenCalled();
    expect(screen.queryByText(/already in your library/)).not.toBeInTheDocument();
  });

  it("offers nothing while a source is being processed", () => {
    const { container } = render(<SourceActions status="processing" onRetry={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("SourceActions failures", () => {
  it("says why a retry was refused", () => {
    render(
      <SourceActions
        status="failed"
        onRetry={() => {}}
        error={{
          detail: { code: "ingesting", message: "This source is being ingested right now." },
        }}
      />,
    );
    expect(screen.getByText("This source is being ingested right now.")).toBeInTheDocument();
  });

  it("shows a plain refusal message too", () => {
    render(
      <SourceActions
        status="done"
        onRetry={() => {}}
        error={{ detail: "URL ingestion is disabled" }}
      />,
    );
    expect(screen.getByText("URL ingestion is disabled")).toBeInTheDocument();
  });

  it("offers nothing for a web source, which v0 cannot ingest", () => {
    const { container } = render(<SourceActions status="failed" kind="url" onRetry={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});
