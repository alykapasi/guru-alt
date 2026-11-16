import { Suspense } from "react";
import { Outlet } from "react-router-dom";

/** A shell's page slot. Pages load on demand (App.tsx), so the boundary sits here, inside the
 * shell: the bar stays put and only the page area says it is loading. */
export function LazyOutlet() {
  return (
    <Suspense fallback={<p className="text-caption text-base-content/70 p-4">Loading…</p>}>
      <Outlet />
    </Suspense>
  );
}
