import { useState } from "react";
import { API_BASE_URL } from "../api/client";
import { useEraseAccount, useRestoreAccount } from "../api/hooks";
import { useSignOutEverywhere } from "../auth/session";

/** A pending-deletion account (S61, V12): the one screen it reaches until restored or erased.
 *
 * Erasing signs out of Clerk as well as Guru: the account is gone, and a Clerk session left
 * standing would only try to exchange for it again. */
export function Recovery({ dueAt }: { dueAt: string }) {
  const restore = useRestoreAccount();
  const erase = useEraseAccount();
  const signOut = useSignOutEverywhere();
  const [confirming, setConfirming] = useState(false);
  const when = new Date(dueAt).toLocaleDateString(undefined, { dateStyle: "long" });

  return (
    <div className="bg-base-100 flex min-h-svh items-center justify-center p-6">
      <div className="flex max-w-md flex-col gap-4">
        <h1 className="text-h1">Your account is scheduled for deletion</h1>
        <p className="text-body text-base-content/70">
          Everything will be erased on {when}. Until then you can restore it exactly as it was.
        </p>
        <button
          type="button"
          className="btn btn-primary"
          disabled={restore.isPending}
          onClick={() => restore.mutate()}
        >
          Restore my account
        </button>
        <a className="link text-body" href={`${API_BASE_URL}/api/v1/me/export`}>
          Download your data (JSON, with a link to each uploaded file)
        </a>
        {confirming ? (
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="btn btn-error btn-sm"
              disabled={erase.isPending}
              onClick={() => erase.mutate(undefined, { onSuccess: () => void signOut() })}
            >
              Yes, erase everything now
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => setConfirming(false)}
            >
              Keep it until {when}
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="btn btn-outline btn-sm"
            onClick={() => setConfirming(true)}
          >
            Erase now
          </button>
        )}
        {(restore.error || erase.error) && (
          <p className="text-caption text-error">Something went wrong. Please try again.</p>
        )}
      </div>
    </div>
  );
}
