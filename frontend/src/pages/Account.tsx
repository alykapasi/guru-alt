import { useState } from "react";
import { UserRound } from "lucide-react";
import { useMemorySetting, useRequestDeletion, useSetMemorySetting } from "../api/hooks";
import { useSignOutEverywhere } from "../auth/session";
import { YourData } from "../components/account/YourData";
import { PreferenceControls } from "../components/preferences/PreferenceControls";

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
          Everything Guru holds about you, as JSON, and each file you uploaded.
        </p>
        <YourData />
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="text-h2">Preferences</h2>
        <p className="text-body text-base-content/70">
          How Guru teaches you everywhere. You can override any of these for a single subject from
          its lesson plan.
        </p>
        <PreferenceControls />
        <MemorySwitch />
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

/** Memory is not a teaching setting and has no per-subject override (S43). Pausing stops
 * learning; what is already remembered stays until it is forgotten. */
function MemorySwitch() {
  const { data } = useMemorySetting();
  const set = useSetMemorySetting();
  const remember = data?.remember ?? true;
  return (
    <label className="flex items-start gap-3 pt-2">
      <input
        type="checkbox"
        className="toggle toggle-sm mt-1"
        checked={remember}
        disabled={set.isPending}
        onChange={(e) => set.mutate(e.target.checked)}
      />
      <span className="flex flex-col">
        <span className="text-body">Remember things from my conversations</span>
        <span className="text-caption text-base-content/60">
          When this is off, Guru stops learning new things about you. What it already remembers
          stays until you forget it on the Memory page.
        </span>
      </span>
    </label>
  );
}
