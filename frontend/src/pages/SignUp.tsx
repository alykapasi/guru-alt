import { Navigate } from "react-router-dom";
import { useCurrentLearner } from "../api/auth";
import { AuthLayout } from "../auth/AuthLayout";
import { ClerkSignUpPanel } from "../auth/ClerkPanels";

/** Create an account (S21).
 *
 * A page, not a bare panel. The route used to render `ClerkSignUpPanel` directly, so registration
 * had no logo, no theme toggle and no heading — the component tests passed because the component
 * was fine; it was the page around it that did not exist.
 *
 * Guru is invite-controlled, so finishing here is not the same as getting in: the exchange inside
 * the panel is where Guru decides, and it says so when the answer is no. The copy sets that
 * expectation rather than letting an invitation refusal arrive as a surprise after the work of
 * creating an account.
 */
export function SignUp() {
  const { data: learner, isPending } = useCurrentLearner();

  if (isPending) {
    return (
      <div className="bg-base-100 text-base-content/70 text-body flex min-h-svh items-center justify-center">
        Checking your session…
      </div>
    );
  }
  if (learner) return <Navigate to="/app/chat" replace />;

  return (
    <AuthLayout
      title="Create your account"
      lede="Guru is invite-only while it is in alpha."
      footer={
        <p className="text-caption text-base-content/50 max-w-sm text-center">
          You will need an invitation for the address you sign up with. Without one, your account is
          created but Guru will not let you in.
        </p>
      }
    >
      <ClerkSignUpPanel />
    </AuthLayout>
  );
}
