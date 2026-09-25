import "@testing-library/jest-dom/vitest";

// jsdom implements no layout, so it has no scrollIntoView. Components that keep a view
// pinned to the newest content call it on mount, and an unimplemented method is a thrown
// TypeError rather than a no-op — which fails tests about behaviour that has nothing to do
// with scrolling. A stub is the honest fix: there is no scroll position here to assert.
Element.prototype.scrollIntoView = () => {};

// jsdom implements no media queries either, and `window.matchMedia` is missing rather than
// inert — so anything rendering the theme toggle throws before it can be asserted on. That is
// why no page test had ever rendered the product chrome, and why a registration route with no
// chrome at all went unnoticed. Reporting "no preference" is the honest default: a test asserts
// what the markup is, not what the operating system would have asked for.
window.matchMedia = ((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
  dispatchEvent: () => false,
})) as typeof window.matchMedia;
