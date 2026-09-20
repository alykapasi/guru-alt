import { useState } from "react";
import { Share2 } from "lucide-react";
import {
  useCancelPublication,
  usePublications,
  useRequestPublication,
  type Publication,
} from "../api/publications";

/** The author's side of publication (S25b).
 *
 * The panel exists to answer two questions without the learner having to ask: can I share this,
 * and what happened to the last time I tried. A "Publish" button that simply fails with a
 * refusal they have to interpret is the version of this that generates support questions. */

function statusLine(publication: Publication): string {
  switch (publication.status) {
    case "pending":
      return "Waiting for review.";
    case "approved":
      return "Approved and shared.";
    case "rejected":
      return "Not approved.";
    case "cancelled":
      return "You withdrew this request.";
  }
}

export interface PublishPanelProps {
  subjectId: string;
  /** Set when the subject was built from the learner's own uploads (S25b D4). */
  sourceDerived: boolean;
}

export function PublishPanel({ subjectId, sourceDerived }: PublishPanelProps) {
  const publications = usePublications(subjectId);
  const request = useRequestPublication(subjectId);
  const cancel = useCancelPublication(subjectId);
  const [note, setNote] = useState("");

  const history = publications.data ?? [];
  const pending = history.find((p) => p.status === "pending") ?? null;
  const latest = history[0] ?? null;

  function submit(event: React.FormEvent) {
    event.preventDefault();
    request.mutate(note.trim() || null, { onSuccess: () => setNote("") });
  }

  return (
    <section className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <Share2 size={18} className="text-primary shrink-0" />
        <h2 className="text-h2">Share this subject</h2>
      </div>

      {sourceDerived ? (
        <p className="text-body text-base-content/70">
          This subject was built from your uploaded material, which stays private. To share a
          subject like this one, build it again without uploads.
        </p>
      ) : (
        <p className="text-caption text-base-content/60">
          An administrator reviews everything that would be shared — the topics, the components and
          your questions with their answers. Approving copies it; your own subject stays yours and
          stays private.
        </p>
      )}

      {!sourceDerived && pending === null && (
        <form onSubmit={submit} className="flex flex-col gap-2">
          <label className="text-caption text-base-content/60" htmlFor="publish-note">
            Anything the reviewer should know (optional)
          </label>
          <textarea
            id="publish-note"
            className="textarea textarea-bordered text-body"
            rows={2}
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <button
            type="submit"
            className="btn btn-primary btn-sm self-start"
            disabled={request.isPending}
          >
            Request publication
          </button>
        </form>
      )}

      {request.error && (
        <p className="text-caption text-error" role="alert">
          {request.error.message}
        </p>
      )}
      {cancel.error && (
        <p className="text-caption text-error" role="alert">
          {cancel.error.message}
        </p>
      )}

      {publications.isLoading ? (
        <p className="text-caption text-base-content/50">Loading…</p>
      ) : latest === null ? (
        <p className="text-caption text-base-content/50">You have not asked to share this yet.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {history.map((publication) => (
            <li key={publication.id} className="border-base-300 flex flex-col gap-1 border-t pt-2">
              <span className="text-body">{statusLine(publication)}</span>
              {publication.review_note && (
                <p className="text-caption text-base-content/70">
                  Reviewer: {publication.review_note}
                </p>
              )}
              {publication.status === "pending" && (
                <button
                  type="button"
                  className="btn btn-ghost btn-xs self-start"
                  disabled={cancel.isPending}
                  onClick={() => cancel.mutate(publication.id)}
                >
                  Withdraw request
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
