import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { Logo } from "../components/Logo";
import { ThemeToggle } from "../components/ThemeToggle";
import { AuthFailed, useCurrentLearner, useDevLogin, useLogin, useRegister } from "../api/auth";

type Mode = "signin" | "register";

/** Sign in, or create an account (S21).
 *
 * One page for both, because they are the same three fields and the same failure, and a
 * person who guesses wrong about whether they already have an account should not have to
 * navigate to find out — the error tells them and the toggle is right there. */
export function SignIn() {
  const location = useLocation();
  const navigate = useNavigate();
  const { data: learner, isPending } = useCurrentLearner();

  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [problem, setProblem] = useState<string | null>(null);

  const login = useLogin();
  const register = useRegister();
  const devLogin = useDevLogin();
  const busy = login.isPending || register.isPending || devLogin.isPending;

  // Where they were headed before being sent here, so signing in resumes rather than dumps
  // them on a default page.
  const next = (location.state as { from?: string } | null)?.from ?? "/app/chat";

  if (isPending) return <Centered>Checking your session…</Centered>;
  if (learner) return <Navigate to={next} replace />;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setProblem(null);
    try {
      if (mode === "signin") {
        await login.mutateAsync({ email, password });
      } else {
        await register.mutateAsync({
          email,
          password,
          display_name: displayName.trim() || undefined,
        });
      }
      navigate(next, { replace: true });
    } catch (error) {
      setProblem(error instanceof AuthFailed ? error.message : "Something went wrong. Try again.");
    }
  }

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
        <div className="flex flex-col gap-2">
          <h1 className="text-h2 text-base-content">
            {mode === "signin" ? "Sign in" : "Create an account"}
          </h1>
          <p className="text-body text-base-content/70">
            {mode === "signin"
              ? "Your goals, plans and practice history are tied to your account."
              : "One account holds your subjects, mastery and notes."}
          </p>
        </div>

        <form className="flex flex-col gap-4" onSubmit={submit}>
          {mode === "register" && (
            <label className="flex flex-col gap-1.5">
              <span className="text-caption text-base-content/70">Name (optional)</span>
              <input
                className="input input-bordered w-full"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                autoComplete="name"
              />
            </label>
          )}

          <label className="flex flex-col gap-1.5">
            <span className="text-caption text-base-content/70">Email</span>
            <input
              className="input input-bordered w-full"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
            />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="text-caption text-base-content/70">Password</span>
            <input
              className="input input-bordered w-full"
              type="password"
              required
              minLength={mode === "register" ? 12 : undefined}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === "signin" ? "current-password" : "new-password"}
            />
            {mode === "register" && (
              <span className="text-caption text-base-content/50">At least 12 characters.</span>
            )}
          </label>

          {problem && (
            <p role="alert" className="text-caption text-error">
              {problem}
            </p>
          )}

          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy && <Loader2 size={16} className="animate-spin" />}
            {mode === "signin" ? "Sign in" : "Create account"}
          </button>
        </form>

        <p className="text-caption text-base-content/70">
          {mode === "signin" ? "No account yet?" : "Already have an account?"}{" "}
          <button
            type="button"
            className="link link-primary"
            onClick={() => {
              setMode(mode === "signin" ? "register" : "signin");
              setProblem(null);
            }}
          >
            {mode === "signin" ? "Create one" : "Sign in"}
          </button>
        </p>

        {import.meta.env.DEV && (
          <button
            type="button"
            className="btn btn-ghost btn-sm self-start"
            onClick={signInAsDeveloper}
            disabled={busy}
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
