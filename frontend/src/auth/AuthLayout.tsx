import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Logo } from "../components/Logo";
import { ThemeToggle } from "../components/ThemeToggle";

/** The frame both auth pages sit in (S21).
 *
 * Shared rather than written twice because the two pages are one moment to whoever is looking
 * at them: the sign-up page used to route straight to the bare Clerk component, so it had no
 * logo, no theme toggle and no heading — a third-party card floating on an empty page, which
 * is what happens when the only thing tested is the component and not the route.
 *
 * The heading lives here, not in the panel, so there is exactly one of it on the page.
 */
export function AuthLayout({
  title,
  lede,
  children,
  footer,
}: {
  title: string;
  lede: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div className="bg-base-100 flex min-h-svh flex-col">
      <header className="mx-auto flex h-16 w-full max-w-[1280px] items-center justify-between px-6">
        <Link to="/" aria-label="Guru home">
          <Logo />
        </Link>
        <ThemeToggle />
      </header>

      {/* Centred in what is left below the header, rather than in the viewport — centring on
          the viewport puts the card behind the header on a short window. */}
      <main className="flex grow items-center justify-center px-6 py-10">
        <div className="flex w-full max-w-sm flex-col items-center gap-6">
          <div className="flex flex-col items-center gap-2 text-center">
            <h1 className="text-h2 text-base-content">{title}</h1>
            <p className="text-body text-base-content/70">{lede}</p>
          </div>

          <div className="border-base-300 bg-base-100 rounded-box w-full border p-6 shadow-sm">
            {children}
          </div>

          {footer}
        </div>
      </main>
    </div>
  );
}
