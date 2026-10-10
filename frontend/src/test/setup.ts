import "@testing-library/jest-dom/vitest";
import { viewport } from "./viewport";

// jsdom implements no media queries, and `window.matchMedia` is missing rather than
// inert — so anything rendering the theme toggle throws before it can be asserted on. That is
// why no page test had ever rendered the product chrome, and why a registration route with no
// chrome at all went unnoticed. Reporting "no preference" is the honest default: a test asserts
// what the markup is, not what the operating system would have asked for.
window.matchMedia = ((query: string) => ({
  matches: query === "(min-width: 1024px)" ? viewport.wide : false,
  media: query,
  onchange: null,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
  dispatchEvent: () => false,
})) as typeof window.matchMedia;

// jsdom implements <dialog> as a plain element: no showModal, no close. The shim does what the
// browser does to the markup — the `open` attribute, and `close` firing a "close" event — so a
// test can assert that a dialog opened modally and that closing it reached the component.
HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
  this.setAttribute("open", "");
  this.dataset.modal = "true";
};
HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
  if (!this.hasAttribute("open")) return;
  this.removeAttribute("open");
  delete this.dataset.modal;
  this.dispatchEvent(new Event("close"));
};
