import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { useCurrentLearner, useDevLogin } from "../api/auth";
import { AuthLayout } from "../auth/AuthLayout";
import { ClerkSignInPanel } from "../auth/ClerkPanels";
import { clerkEnabled } from "../auth/mode";

/** Sign in (S21).
 *
 * Guru holds no credentials any more: Clerk owns the password, the recovery mail and the social
 * providers, and this page is where its panel is mounted. The exchange inside that panel turns
 * a Clerk session into Guru's own.
 *
 * A build with no publishable key has no sign-in at all, and says so plainly rather than showing
 * a form that cannot work. That is not a broken deployment — it is what vitest, CI, the browser
 * journeys and a checkout with nobody's Clerk account all run, and in development the button
 * below is the way in.
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
    <AuthLayout
      title="Welcome back"
      lede="Sign in to pick up where you left off."
      footer={
        <DeveloperEntrance
          onClick={signInAsDeveloper}
          disabled={devLogin.isPending}
          problem={problem}
        />
      }
    >
      {clerkEnabled ? (
        <ClerkSignInPanel />
      ) : (
        <div className="flex flex-col gap-2">
          <p className="text-body text-base-content">Sign-in is not configured for this build.</p>
          <p className="text-caption text-base-content/60">
            No identity provider key is set, so there is no sign-in form to show. In development,
            use the button below.
          </p>
        </div>
      )}
    </AuthLayout>
  );
}

/** The way in when there is no provider — a real button, and only ever in a dev build. */
function DeveloperEntrance({
  onClick,
  disabled,
  problem,
}: {
  onClick: () => void;
  disabled: boolean;
  problem: string | null;
}) {
  if (!import.meta.env.DEV) return null;
  return (
    <div className="flex w-full flex-col items-center gap-2">
      <div className="border-base-300 w-full border-t" />
      <p className="text-caption text-base-content/50">Development only</p>
      <button
        type="button"
        className="btn btn-outline btn-sm"
        onClick={onClick}
        disabled={disabled}
      >
        Development sign-in
      </button>
      {problem && (
        <p role="alert" className="text-caption text-error">
          {problem}
        </p>
      )}
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
