import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { SignIn, SignUp, UserButton, useAuth, useClerk } from "@clerk/react";
import { ExchangeFailed, useCurrentLearner, useExchange, useLogout } from "../api/auth";

/** Clerk's hosted panels, and the exchange that turns a Clerk session into a Guru one (S21).
 *
 * Every component here calls a Clerk hook, so nothing in this file may be imported by a build
 * without a publishable key — `ClerkProvider` is not mounted there and the hooks would throw.
 * Callers gate on `clerkEnabled` from `./mode`, which is decided once at module load.
 */

/** Guru's session, obtained once from Clerk's (S21).
 *
 * Clerk proves who someone is; Guru decides whether they may come in. The two answers are
 * different and the refusals are handled differently, which is the whole reason this is a
 * visible exchange rather than a token passed on every request.
 */
function useExchangeOnce() {
  const { isLoaded, isSignedIn, getToken } = useAuth();
  const { data: learner, isPending } = useCurrentLearner();
  const exchange = useExchange();
  const clerk = useClerk();
  const [problem, setProblem] = useState<string | null>(null);
  // Guards against React 19's double-invoked effects in development and against a re-render
  // landing mid-flight. Without it the exchange fires twice, and the second one races the
  // first's `queryClient.clear()`.
  const attempted = useRef(false);

  useEffect(() => {
    if (!isLoaded || !isSignedIn || isPending || learner || attempted.current) return;
    attempted.current = true;
    void (async () => {
      try {
        const token = await getToken();
        if (!token) throw new ExchangeFailed(401, "Clerk did not issue a session token.");
        await exchange.mutateAsync(token);
      } catch (error) {
        if (!(error instanceof ExchangeFailed)) {
          setProblem("Something went wrong signing you in.");
          return;
        }
        setProblem(error.message);
        // A refusal is about *this person* — not invited, suspended, ambiguous — so leaving
        // Clerk signed in would loop them through a doomed exchange on every page load. A 5xx
        // is Guru failing to ask, which says nothing about them: keep their Clerk session so
        // they can retry, because signing them out would make an outage look like a rejection.
        //
        // `attempted` stays set either way. Re-arming it here would retry a refusal that is
        // not going to change on its own, as fast as the component re-renders.
        if (error.status < 500) await clerk.signOut();
      }
    })();
  }, [isLoaded, isSignedIn, isPending, learner, getToken, exchange, clerk]);

  return problem;
}

function Problem({ children }: { children: string }) {
  return (
    <p role="alert" className="text-error text-body mt-4 max-w-sm text-center">
      {children}
    </p>
  );
}

export function ClerkSignInPanel() {
  const problem = useExchangeOnce();
  return (
    <div className="flex flex-col items-center">
      <SignIn routing="path" path="/signin" signUpUrl="/sign-up" />
      {problem && <Problem>{problem}</Problem>}
    </div>
  );
}

export function ClerkSignUpPanel() {
  const problem = useExchangeOnce();
  return (
    <div className="flex flex-col items-center">
      <SignUp routing="path" path="/sign-up" signInUrl="/signin" />
      {problem && <Problem>{problem}</Problem>}
    </div>
  );
}

/** Ends Guru's session when Clerk's ends elsewhere (S21).
 *
 * Signing out in another tab, or Clerk expiring the session, must not leave this tab holding a
 * working Guru cookie — the two sessions have to agree, and Clerk is the one that decides.
 */
export function ClerkSessionWatcher() {
  const { isLoaded, isSignedIn } = useAuth();
  const { data: learner } = useCurrentLearner();
  const logout = useLogout();
  const navigate = useNavigate();

  useEffect(() => {
    if (!isLoaded || isSignedIn || !learner) return;
    void (async () => {
      await logout.mutateAsync();
      navigate("/signin", { replace: true });
    })();
  }, [isLoaded, isSignedIn, learner, logout, navigate]);

  return null;
}

export function ClerkUserButton() {
  // Where sign-out lands is set once on `ClerkProvider` in `main.tsx`; this prop was removed
  // from `UserButton` in Clerk v6.
  return <UserButton />;
}
