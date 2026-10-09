/** The first thing a keyboard user reaches: past the navigation to the page's own content. */
export function SkipLink() {
  return (
    <a
      href="#main"
      className="focus:bg-base-100 focus:text-primary sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded-field focus:px-3 focus:py-2"
    >
      Skip to content
    </a>
  );
}
