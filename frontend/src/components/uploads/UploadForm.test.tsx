import { expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { UploadForm } from "./UploadForm";

it("offers file uploads without URL ingestion in v0", () => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <UploadForm subjects={[]} />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("button", { name: "Upload a file" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "Add link" })).not.toBeInTheDocument();
  expect(screen.queryByPlaceholderText("https://…")).not.toBeInTheDocument();
});
