import { useState } from "react";
import { UserRound } from "lucide-react";
import { API_BASE_URL } from "../api/client";
import { useRequestDeletion } from "../api/hooks";
import { useSignOutEverywhere } from "../auth/session";

/** The learner's account: take their data, or delete it (S61, V12).
 *
 * Deleting is recoverable for seven days by signing in again; "Erase now instead" skips the
 * window. Either way every session ends at once, so both sign out of Clerk too. */
export function Account() {
  const requestDeletion = useRequestDeletion();
  const signOut = useSignOutEverywhere();
  const [confirming, setConfirming] = useState(false);

  function remove(now: boolean) {
    requestDeletion.mutate({ now }, { onSuccess: () => void signOut() });
  }

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-6 py-8">
      <header className="flex flex-col gap-2">
        <h1 className="text-h1 flex items-center gap-2">
          <UserRound size={20} className="text-primary" aria-hidden />
          Your account
        </h1>
      </header>

      <section className="flex flex-col gap-2">
        <h2 className="text-h2">Your data</h2>
        <p className="text-body text-base-content/70">
          Everything Guru holds about you, as JSON, with a link to each file you uploaded.
        </p>
        <a className="link text-body" href={`${API_BASE_URL}/api/v1/me/export`}>
          Download your data
        </a>
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="text-h2">Delete account</h2>
        <p className="text-body text-base-content/70">
          Your account will be closed at once and erased after 7 days. Signing in again before then
          lets you restore it.
        </p>
        {confirming ? (
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className="btn btn-error btn-sm"
              disabled={requestDeletion.isPending}
              onClick={() => remove(false)}
            >
              Delete my account
            </button>
            <button
              type="button"
              className="btn btn-outline btn-error btn-sm"
              disabled={requestDeletion.isPending}
              onClick={() => remove(true)}
            >
              Erase now instead
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => setConfirming(false)}
            >
              Cancel
            </button>
          </div>
        ) : (
          <div>
            <button
              type="button"
              className="btn btn-outline btn-error btn-sm"
              onClick={() => setConfirming(true)}
            >
              Delete account…
            </button>
          </div>
        )}
        {requestDeletion.error && (
          <p className="text-caption text-error">Something went wrong. Please try again.</p>
        )}
      </section>
    </div>
  );
}
