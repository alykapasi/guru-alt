import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LoadMore } from "./LoadMore";

/** Every paged list (S62) needs a way to the rows past its first page, or they are unreachable. */
describe("loading the next page of a list", () => {
  it("offers more when the server says there is more", async () => {
    const fetchNextPage = vi.fn();
    render(<LoadMore hasNextPage isFetchingNextPage={false} fetchNextPage={fetchNextPage} />);
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(fetchNextPage).toHaveBeenCalledTimes(1);
  });

  it("offers nothing at the end of the list", () => {
    render(<LoadMore hasNextPage={false} isFetchingNextPage={false} fetchNextPage={vi.fn()} />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("cannot be pressed twice while a page is on its way", () => {
    render(<LoadMore hasNextPage isFetchingNextPage fetchNextPage={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Loading…" })).toBeDisabled();
  });
});
