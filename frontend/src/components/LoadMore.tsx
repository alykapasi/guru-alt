/** "Load more" under a paged list (S62): present only while the server says more exists. */
export function LoadMore({
  hasNextPage,
  isFetchingNextPage,
  fetchNextPage,
  className = "btn btn-ghost btn-xs mt-1 w-full",
}: {
  hasNextPage: boolean | undefined;
  isFetchingNextPage: boolean;
  fetchNextPage: () => unknown;
  className?: string;
}) {
  if (!hasNextPage) return null;
  return (
    <button className={className} onClick={() => fetchNextPage()} disabled={isFetchingNextPage}>
      {isFetchingNextPage ? "Loading…" : "Load more"}
    </button>
  );
}
