import { afterEach } from "vitest";

/** Which side of the one layout breakpoint a test renders on. Narrow by default — the setup
 * file's "no preference" stance — and reset after every test so one file's choice does not
 * leak into the next. */
export const viewport = { wide: false };

export function setViewportWide(wide: boolean): void {
  viewport.wide = wide;
}

afterEach(() => {
  viewport.wide = false;
});
