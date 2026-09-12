import "@testing-library/jest-dom/vitest";

// jsdom implements no layout, so it has no scrollIntoView. Components that keep a view
// pinned to the newest content call it on mount, and an unimplemented method is a thrown
// TypeError rather than a no-op — which fails tests about behaviour that has nothing to do
// with scrolling. A stub is the honest fix: there is no scroll position here to assert.
Element.prototype.scrollIntoView = () => {};
