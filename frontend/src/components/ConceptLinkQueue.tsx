import { useState } from "react";
import { useConceptLinkQueue, useConceptLinkVerdict } from "../api/admin";
import type { components } from "../api/schema";

type Row = components["schemas"]["ConceptLinkReviewRead"];

/** Pairs of curated components that share a concept name, for an administrator to rule on
 * (S24). An endorsement is half a link — learners still choose — so the reason is required:
 * it is what a learner reads when deciding. Decided rows stay listed with their verdict. */
export function ConceptLinkQueue() {
  const { data, isLoading, isError } = useConceptLinkQueue();
  if (isLoading) return <p className="text-caption text-base-content/50">Loading concept links…</p>;
  // Checked before the empty case, and by shape rather than truthiness: a query error or an
  // unexpected response body is still a truthy, non-array `data` in the second case, and
  // `.map`-ing it below would take down the whole Admin page — there is no error boundary here.
  if (isError || !Array.isArray(data)) {
    return (
      <p className="text-caption text-error" role="alert">
        Couldn't load concept links. Refresh to try again.
      </p>
    );
  }
  if (data.length === 0) {
    return <p className="text-caption text-base-content/50">No concept links to review.</p>;
  }
  return (
    <div className="flex flex-col gap-2">
      {data.map((row) => (
        <LinkRow key={row.id} row={row} />
      ))}
    </div>
  );
}

function LinkRow({ row }: { row: Row }) {
  const [reason, setReason] = useState("");
  const verdict = useConceptLinkVerdict();
  const send = (endorse: boolean) =>
    verdict.mutate({ linkId: row.id, endorse, reason: reason.trim() });
  return (
    <div className="rounded-field bg-base-200 flex flex-col gap-2 px-3 py-2">
      <p className="text-body">
        {row.kc_a_name} ({row.subject_a_name}) ↔ {row.kc_b_name} ({row.subject_b_name})
      </p>
      {row.verdict ? (
        <p className="text-caption text-base-content/60">
          {row.verdict === "endorsed" ? "Endorsed" : "Rejected"} — {row.reason}
        </p>
      ) : (
        <div className="flex items-end gap-2">
          <label className="flex flex-1 flex-col gap-1">
            <span className="text-caption">Reason</span>
            <input
              className="input input-sm"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!reason.trim() || verdict.isPending}
            onClick={() => send(true)}
          >
            Endorse
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={!reason.trim() || verdict.isPending}
            onClick={() => send(false)}
          >
            Reject
          </button>
        </div>
      )}
      {/* A fixed sentence, not the server's own: openapi-fetch throws the raw {detail} body on
          error, and rendering it here would print "undefined" rather than help. */}
      {verdict.error && (
        <p className="text-caption text-error" role="alert">
          Couldn't save that verdict. Refresh and try again.
        </p>
      )}
    </div>
  );
}
