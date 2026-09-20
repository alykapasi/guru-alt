import { useState } from "react";
import {
  useApprovePublication,
  useRejectPublication,
  useReviewQueue,
  useWithdrawSubject,
  type PublicationReview,
  type Snapshot,
} from "../api/publications";

/** The reviewer's queue (S25b D3).
 *
 * The reviewer is deciding what enters the shared library, so this shows what would actually
 * ship — the graph, and every question with its answer key. A queue that showed a summary
 * would be asking somebody to approve something they had not read. */

function GraphOutline({ snapshot }: { snapshot: Snapshot }) {
  return (
    <ul className="flex flex-col gap-1">
      {snapshot.topics.map((topic) => (
        <li key={topic.id}>
          <span className="text-body font-semibold">{topic.name}</span>
          <ul className="text-caption text-base-content/70 ml-4 list-disc">
            {snapshot.kcs
              .filter((kc) => kc.topic_id === topic.id)
              .map((kc) => (
                <li key={kc.id}>{kc.name}</li>
              ))}
          </ul>
        </li>
      ))}
    </ul>
  );
}

function ReviewCard({ publication }: { publication: PublicationReview }) {
  const approve = useApprovePublication();
  const reject = useRejectPublication();
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [note, setNote] = useState("");

  const snapshot = publication.snapshot;

  function toggle(itemId: string) {
    setExcluded((current) => {
      const next = new Set(current);
      if (next.has(itemId)) next.delete(itemId);
      else next.add(itemId);
      return next;
    });
  }

  return (
    <article className="card bg-base-100 border-base-300 flex flex-col gap-3 border p-4">
      <header className="flex flex-col gap-1">
        <h3 className="text-h3">{snapshot.subject.name}</h3>
        <p className="text-caption text-base-content/60">
          Requested by {publication.author_handle ?? "a closed account"}
        </p>
        {publication.author_note && <p className="text-body">{publication.author_note}</p>}
      </header>

      <GraphOutline snapshot={snapshot} />

      {snapshot.items.length > 0 && (
        <section className="flex flex-col gap-2">
          <h4 className="text-caption text-base-content/50">
            Questions ({snapshot.items.length}) — untick any that should not be shared
          </h4>
          <ul className="flex flex-col gap-2">
            {snapshot.items.map((item) => (
              <li key={item.id} className="flex items-start gap-2">
                <input
                  type="checkbox"
                  className="checkbox checkbox-sm mt-1"
                  checked={!excluded.has(item.id)}
                  onChange={() => toggle(item.id)}
                  aria-label={`Include: ${item.stem}`}
                />
                <div className="flex flex-col">
                  <span className="text-body">{item.stem}</span>
                  {item.answer_key && (
                    <code className="text-caption text-base-content/60">
                      {JSON.stringify(item.answer_key)}
                    </code>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      <label className="text-caption text-base-content/60" htmlFor={`note-${publication.id}`}>
        Note to the author
      </label>
      <textarea
        id={`note-${publication.id}`}
        className="textarea textarea-bordered text-body"
        rows={2}
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="btn btn-primary btn-sm"
          disabled={approve.isPending}
          onClick={() =>
            approve.mutate({
              publicationId: publication.id,
              excludedItemIds: [...excluded],
              note: note.trim() || null,
            })
          }
        >
          Approve and share
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          disabled={reject.isPending || note.trim().length === 0}
          onClick={() => reject.mutate({ publicationId: publication.id, note: note.trim() })}
        >
          Reject
        </button>
        {/* Disabled rather than hidden, with the reason stated: a rejection the author cannot
            act on is one they will simply send again. */}
        {note.trim().length === 0 && (
          <span className="text-caption text-base-content/50 self-center">
            Rejecting needs a note.
          </span>
        )}
      </div>

      {approve.error && (
        <p className="text-caption text-error" role="alert">
          {approve.error.message}
        </p>
      )}
      {reject.error && (
        <p className="text-caption text-error" role="alert">
          {reject.error.message}
        </p>
      )}
    </article>
  );
}

function PublishedRow({ publication }: { publication: PublicationReview }) {
  const withdraw = useWithdrawSubject();
  const [reason, setReason] = useState("");
  const [asking, setAsking] = useState(false);
  const subjectId = publication.published_subject_id;

  if (subjectId === null) return null;

  return (
    <li className="border-base-300 flex flex-col gap-2 border-t py-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-body">{publication.snapshot.subject.name}</span>
        <button
          type="button"
          className="btn btn-ghost btn-xs"
          onClick={() => setAsking((open) => !open)}
        >
          Withdraw
        </button>
      </div>
      {asking && (
        <div className="flex flex-wrap items-start gap-2">
          <input
            className="input input-bordered input-sm grow"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            aria-label={`Why withdraw ${publication.snapshot.subject.name}?`}
            placeholder="Why it is being unlisted"
          />
          <button
            type="button"
            className="btn btn-sm"
            disabled={withdraw.isPending || reason.trim().length === 0}
            onClick={() =>
              withdraw.mutate(
                { subjectId, reason: reason.trim() },
                { onSuccess: () => setAsking(false) },
              )
            }
          >
            Unlist it
          </button>
        </div>
      )}
      {withdraw.error && (
        <p className="text-caption text-error" role="alert">
          {withdraw.error.message}
        </p>
      )}
    </li>
  );
}

function Published() {
  const approved = useReviewQueue("approved");
  const live = (approved.data ?? []).filter((p) => p.published_subject_id !== null);

  if (approved.isLoading) {
    return <p className="text-caption text-base-content/50">Loading…</p>;
  }
  if (live.length === 0) {
    return <p className="text-caption text-base-content/50">Nothing has been shared yet.</p>;
  }
  return (
    <ul className="flex flex-col">
      {live.map((publication) => (
        <PublishedRow key={publication.id} publication={publication} />
      ))}
    </ul>
  );
}

export function PublicationQueue() {
  const queue = useReviewQueue("pending");

  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-h2">Waiting for review</h2>
      <p className="text-caption text-base-content/60">
        Approving copies the subject into the shared library, exactly as it is shown here. Nothing
        the author has changed since asking is included.
      </p>

      {queue.isLoading ? (
        <p className="text-caption text-base-content/50">Loading…</p>
      ) : queue.isError || !queue.data ? (
        <p className="text-body text-error">Could not read the review queue.</p>
      ) : queue.data.length === 0 ? (
        <p className="text-caption text-base-content/50">Nothing is waiting for review.</p>
      ) : (
        <div className="flex flex-col gap-4">
          {queue.data.map((publication) => (
            <ReviewCard key={publication.id} publication={publication} />
          ))}
        </div>
      )}

      <h2 className="text-h2">Shared</h2>
      <p className="text-caption text-base-content/60">
        Withdrawing unlists a subject from the catalog. It stays reachable by anyone already
        studying it — unlisting is not removal.
      </p>
      <Published />
    </section>
  );
}
