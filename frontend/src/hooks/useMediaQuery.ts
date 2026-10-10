import { useSyncExternalStore } from "react";

/** The one layout breakpoint (Tailwind's `lg`). Above it side panels sit beside the content;
 * below it they open as sheets over it (S53). */
export const WIDE_QUERY = "(min-width: 1024px)";

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    () => window.matchMedia(query).matches,
  );
}
