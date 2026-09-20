import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { Logo } from "../components/Logo";
import { ThemeToggle } from "../components/ThemeToggle";
import { useCurrentLearner, useDevLogin } from "../api/auth";
import { ClerkSignInPanel } from "../auth/ClerkPanels";
import { clerkEnabled } from "../auth/mode";

/** Sign in (S21).
 *
 * Guru holds no credentials any more: Clerk owns the password, the recovery mail and the
 * social providers, and this page is where its panel is mounted. The exchange inside that
 * panel turns a Clerk session into Guru's own.
 *
 * A build with no publishable key has no sign-in at all, and says so plainly rather than
 * showing a form that cannot work. That is not a broken deployment — it is what vitest, CI,
 * the browser journeys and a checkout with nobody's Clerk account all run, and in development
 * the button below is the way in.
 */
export function SignIn() {
  const location = useLocation();
  const navigate = useNavigate();
  const { data: learner, isPending } = useCurrentLearner();
  const devLogin = useDevLogin();
  const [problem, setProblem] = useState<string | null>(null);

  // Where they were headed before being sent here, so signing in resumes rather than dumps
  // them on a default page.
  const next = (location.state as { from?: string } | null)?.from ?? "/app/chat";

  if (isPending) return <Centered>Checking your session…</Centered>;
  if (learner) return <Navigate to={next} replace />;

  async function signInAsDeveloper() {
    setProblem(null);
    const result = await devLogin.mutateAsync();
    if (result) navigate(next, { replace: true });
    else setProblem("Development sign-in is not available on this server.");
  }

  return (
    <div className="bg-base-100 min-h-svh">
      <header className="mx-auto flex h-16 max-w-[1280px] items-center justify-between px-6">
        <Logo />
        <ThemeToggle />
      </header>

      <main className="mx-auto flex max-w-md flex-col gap-6 px-6 py-16">
        <h1 className="text-h2 text-base-content">Sign in</h1>

        {clerkEnabled ? (
          <ClerkSignInPanel />
        ) : (
          <p className="text-body text-base-content/70">
            Sign-in is not configured for this build.
          </p>
        )}

        {problem && (
          <p role="alert" className="text-caption text-error">
            {problem}
          </p>
        )}

        {import.meta.env.DEV && (
          <button
            type="button"
            className="btn btn-ghost btn-sm self-start"
            onClick={signInAsDeveloper}
            disabled={devLogin.isPending}
          >
            Development sign-in
          </button>
        )}
      </main>
    </div>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-base-100 text-base-content/70 text-body flex min-h-svh items-center justify-center">
      {children}
    </div>
  );
}
