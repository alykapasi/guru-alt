import { Link2 } from "lucide-react";
import { useConceptLinkSuggestions, useDecideConceptLink } from "../../api/hooks";

/** Ideas this subject shares with another one, and whether to use what the learner showed
 * there (S24). Nothing carries over until they say so: a suggestion is the endorser's half of
 * the agreement, and these buttons are the learner's. Renders nothing when there is nothing to
 * decide. */
export function ConnectionsPanel({ subjectId }: { subjectId: string }) {
  const { data } = useConceptLinkSuggestions();
  const decide = useDecideConceptLink();
  const here = (data ?? []).filter(
    (s) => s.a.subject_id === subjectId || s.b.subject_id === subjectId,
  );
  if (here.length === 0) return null;
  return (
    <section className="flex max-w-2xl flex-col gap-2" aria-label="Connections">
      <h2 className="text-h3 flex items-center gap-2">
        <Link2 size={16} className="text-primary" /> Connections
      </h2>
      {here.map((s) => {
        const [mine, other] = s.a.subject_id === subjectId ? [s.a, s.b] : [s.b, s.a];
        const act = (decision: "accept" | "decline" | "revoke") =>
          decide.mutate({ linkId: s.link_id, decision });
        return (
          <div key={s.link_id} className="rounded-field bg-base-200 flex flex-col gap-1 px-3 py-2">
            <p className="text-body">
              {mine.kc_name} here looks like the same idea as {other.kc_name} in{" "}
              {other.subject_name}.
            </p>
            {s.reason && <p className="text-caption text-base-content/60">{s.reason}</p>}
            <div className="flex gap-1">
              {s.decision === "accepted" ? (
                <button
                  type="button"
                  className="btn btn-ghost btn-xs"
                  disabled={decide.isPending}
                  onClick={() => act("revoke")}
                >
                  Stop using it
                </button>
              ) : (
                <>
                  <button
                    type="button"
                    className="btn btn-primary btn-xs"
                    disabled={decide.isPending}
                    onClick={() => act("accept")}
                  >
                    Use it here
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost btn-xs"
                    disabled={decide.isPending}
                    onClick={() => act("decline")}
                  >
                    Not the same
                  </button>
                </>
              )}
            </div>
          </div>
        );
      })}
      {decide.error && (
        <p className="text-caption text-error" role="alert">
          Couldn't save that choice. Refresh and try again.
        </p>
      )}
    </section>
  );
}
