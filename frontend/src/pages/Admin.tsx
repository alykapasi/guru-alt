import { useState } from "react";
import { Gauge } from "lucide-react";
import {
  useAdminActions,
  useImpersonations,
  useLearnerRoster,
  useSpend,
  useStartVisit,
} from "../api/admin";
import type { components } from "../api/schema";

type Latency = components["schemas"]["Latency"];
type SpendBucket = components["schemas"]["SpendBucket"];
type LearnerUsage = components["schemas"]["LearnerUsage"];

/** What this deployment costs, how long its models take, and who is using it (P10).
 *
 * All three numbers have been recorded per call since Phase 1 — cost and tokens tagged by role
 * and model, latency since S48 — and nothing had ever shown any of them. The failure mode that
 * makes was a bill discovered monthly and a slow turn nobody could attribute.
 *
 * Two things this page refuses to round off, because both are ways of reporting a number that
 * is not the number:
 *
 * - **Cost is a floor, not a figure, when anything unpriced ran.** A NULL price means the model
 *   has no known one, which is not the same as free, and a deployment on entirely unpriced
 *   models would otherwise read as costing nothing at all.
 * - **A percentile is over the calls that carry that timing.** A completion records how long it
 *   took; a stream records how long it took to start. Neither covers all the traffic, so each
 *   is shown with its population and an empty one says "not measured" rather than "0 ms". */

const WINDOWS = [
  { hours: 1, label: "Last hour" },
  { hours: 24, label: "Last 24 hours" },
  { hours: 24 * 7, label: "Last 7 days" },
];

function money(usd: number): string {
  return usd < 0.01 && usd > 0 ? "<$0.01" : `$${usd.toFixed(2)}`;
}

function ms(value: number | null): string {
  if (value === null) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${value}ms`;
}

function ago(iso: string | null): string {
  if (!iso) return "—";
  // Most timestamps here are naive UTC (the timestamp mixin), but the visit's own clock is
  // zoned; a "Z" appended to one that already has a zone is an invalid date.
  const zoned = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  const minutes = Math.round((Date.now() - new Date(zoned ? iso : iso + "Z").getTime()) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)}h ago`;
  return `${Math.round(minutes / (60 * 24))}d ago`;
}

/** A timing and the calls behind it. The count is shown even when it is zero: "no completions
 * were measured" and "completions were instant" are different facts and only one is true. */
function Timing({ label, latency }: { label: string; latency: Latency }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-caption text-base-content/50">{label}</span>
      {latency.calls === 0 ? (
        <span className="text-body text-base-content/40">Not measured</span>
      ) : (
        <span className="text-body tabular-nums">
          p50 {ms(latency.p50_ms)} · p95 {ms(latency.p95_ms)}
        </span>
      )}
      <span className="text-caption text-base-content/40 tabular-nums">
        over {latency.calls} call{latency.calls === 1 ? "" : "s"}
      </span>
    </div>
  );
}

function BucketRows({ buckets }: { buckets: SpendBucket[] }) {
  return (
    <>
      {buckets.map((bucket) => (
        <tr key={bucket.name} className="border-base-300 border-t">
          <td className="py-2 pr-4">{bucket.name}</td>
          <td className="py-2 pr-4 text-right tabular-nums">{bucket.calls}</td>
          <td className="py-2 pr-4 text-right tabular-nums">
            {money(bucket.cost_usd)}
            {bucket.unpriced_calls > 0 && (
              <span className="text-base-content/40" title={`${bucket.unpriced_calls} unpriced`}>
                {" "}
                +
              </span>
            )}
          </td>
          <td className="py-2 pr-4 text-right tabular-nums">
            {bucket.completion.calls === 0 ? "—" : ms(bucket.completion.p95_ms)}
          </td>
          <td className="py-2 text-right tabular-nums">
            {bucket.first_token.calls === 0 ? "—" : ms(bucket.first_token.p95_ms)}
          </td>
        </tr>
      ))}
    </>
  );
}

function RosterRow({
  learner,
  onView,
}: {
  learner: LearnerUsage;
  onView: (learner: LearnerUsage) => void;
}) {
  return (
    <tr className="border-base-300 border-t">
      <td className="py-2 pr-4">
        {learner.display_name || learner.handle}
        {learner.is_admin && (
          <span className="badge badge-ghost badge-sm ml-2 align-middle">admin</span>
        )}
        <span className="text-caption text-base-content/50 block">{learner.email ?? "—"}</span>
      </td>
      <td className="py-2 pr-4 text-right tabular-nums">{learner.calls}</td>
      <td className="py-2 pr-4 text-right tabular-nums">
        {money(learner.cost_usd)}
        {learner.unpriced_calls > 0 && <span className="text-base-content/40"> +</span>}
      </td>
      <td className="py-2 pr-4 text-right tabular-nums">{ago(learner.last_call_at)}</td>
      <td className="py-2 text-right">
        <button type="button" className="btn btn-ghost btn-xs" onClick={() => onView(learner)}>
          View as
        </button>
      </td>
    </tr>
  );
}

/** Asking why, before the credential exists.
 *
 * A dialog rather than a confirm step, because the reason is the point: the API refuses a
 * visit without one, and every row of the log is only as useful as the sentence somebody
 * typed here. Naming the account in the heading is the second job — the mistake this catches
 * is clicking the wrong row. */
function ViewAsDialog({ learner, onClose }: { learner: LearnerUsage; onClose: () => void }) {
  const [reason, setReason] = useState("");
  const start = useStartVisit();

  function submit(event: React.FormEvent) {
    event.preventDefault();
    start.mutate({ learnerId: learner.id, reason }, { onSuccess: onClose });
  }

  return (
    <div className="bg-base-content/40 fixed inset-0 z-50 flex items-center justify-center p-6">
      <form
        onSubmit={submit}
        className="bg-base-100 border-base-300 flex w-full max-w-md flex-col gap-4 rounded-box border p-6"
      >
        <h2 className="text-h2">View {learner.display_name || learner.handle}&rsquo;s account</h2>
        {/* No number: the limit is `GURU_IMPERSONATION_TTL_MINUTES`, and a page that printed
            "15 minutes" would be wrong for any deployment that changed it. */}
        <p className="text-body text-base-content/70">
          Admin access expires on its own. Your actions are recorded against your name, and{" "}
          {learner.display_name || learner.handle} can see it in their own data export.
        </p>
        <label className="flex flex-col gap-1">
          <span className="text-caption text-base-content/70">Why are you looking?</span>
          <input
            className="input input-bordered"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="They report their upload never became a lesson"
            autoFocus
          />
        </label>
        {start.error && <p className="text-caption text-error">{start.error.message}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary btn-sm" disabled={start.isPending}>
            Start viewing
          </button>
        </div>
      </form>
    </div>
  );
}

/** The record of who looked at whom.
 *
 * Shown on the same page as the button that creates the rows, deliberately: an audit an
 * administrator has to go somewhere else to read is one they do not read. `ended_at` is blank
 * for a visit nobody closed — it expired instead, which is what `expires_at` bounds, and
 * printing a wall-clock end nobody performed would be inventing an event. */
function ActionLog({ visitId }: { visitId: string }) {
  const [expanded, setExpanded] = useState(false);
  const actions = useAdminActions(visitId, expanded);
  return (
    <div>
      <button className="btn btn-xs" onClick={() => setExpanded(!expanded)}>
        Action log
      </button>
      {expanded &&
        (actions.isError ? (
          <p>Could not read actions.</p>
        ) : actions.isLoading ? (
          <p>Loading…</p>
        ) : (
          <ul>
            {actions.data?.map((action) => (
              <li key={action.id}>
                {action.method} {action.route} · {action.status_code ?? "incomplete"}
              </li>
            ))}
          </ul>
        ))}
    </div>
  );
}

function AccessLog() {
  const log = useImpersonations();

  if (log.isLoading) return <p className="text-caption text-base-content/50">Loading…</p>;
  if (log.isError || !log.data)
    return <p className="text-body text-error">Could not read the access log.</p>;
  if (!log.data.length)
    return (
      <p className="text-body text-base-content/60">
        Nobody has viewed a learner&rsquo;s account. Every visit is recorded here, and stays
        recorded if viewing is switched off.
      </p>
    );

  return (
    <div className="overflow-x-auto">
      <table className="text-body w-full">
        <thead className="text-caption text-base-content/50 text-left">
          <tr>
            <th className="pb-2 pr-4 font-normal">Administrator</th>
            <th className="pb-2 pr-4 font-normal">Account</th>
            <th className="pb-2 pr-4 font-normal">Reason</th>
            <th className="pb-2 pr-4 font-normal">Started</th>
            <th className="pb-2 font-normal">Ended</th>
            <th className="pb-2 font-normal">Actions</th>
          </tr>
        </thead>
        <tbody>
          {log.data.map((row) => (
            <tr key={row.id} className="border-base-300 border-t">
              <td className="py-2 pr-4">{row.admin_handle}</td>
              {/* Blank when the account has been closed: the id and handle are cleared and the
                  rest of the row is kept, which is the record outliving its subject. */}
              <td className="py-2 pr-4">
                {row.learner_handle ?? <span className="text-base-content/40">account closed</span>}
              </td>
              <td className="py-2 pr-4">{row.reason}</td>
              <td className="py-2 pr-4 tabular-nums">{ago(row.created_at)}</td>
              <td className="py-2 tabular-nums">
                {row.ended_at ? (
                  ago(row.ended_at)
                ) : (
                  <span className="text-base-content/40">expired</span>
                )}
              </td>
              <td className="py-2">
                <ActionLog visitId={row.id} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Admin() {
  const [hours, setHours] = useState(24);
  const [viewing, setViewing] = useState<LearnerUsage | null>(null);
  const spend = useSpend(hours);
  const roster = useLearnerRoster(hours);

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 px-6 py-8">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-2">
          <h1 className="text-h1 flex items-center gap-2">
            <Gauge size={20} className="text-primary" aria-hidden />
            What this deployment is doing
          </h1>
          <p className="text-body text-base-content/70">
            Cost, model latency and who is here. Every number is over the window you pick, and
            refreshes on its own each minute.
          </p>
        </div>
        <select
          className="select select-bordered select-sm"
          aria-label="Window"
          value={hours}
          onChange={(e) => setHours(Number(e.target.value))}
        >
          {WINDOWS.map((w) => (
            <option key={w.hours} value={w.hours}>
              {w.label}
            </option>
          ))}
        </select>
      </header>

      {spend.isLoading ? (
        <p className="text-caption text-base-content/50">Loading…</p>
      ) : spend.isError || !spend.data ? (
        <p className="text-body text-error">Could not read the spend report.</p>
      ) : (
        <>
          <section className="border-base-300 flex flex-wrap gap-8 rounded-box border p-5">
            <div className="flex flex-col gap-1">
              <span className="text-caption text-base-content/50">Model spend</span>
              <span className="text-h2 tabular-nums">{money(spend.data.cost_usd)}</span>
              <span className="text-caption text-base-content/40 tabular-nums">
                {spend.data.calls} call{spend.data.calls === 1 ? "" : "s"}
              </span>
            </div>
            <Timing label="Completion" latency={spend.data.completion} />
            <Timing label="Time to first token" latency={spend.data.first_token} />
            {spend.data.budget_usd !== null && (
              <div className="flex flex-col gap-1">
                <span className="text-caption text-base-content/50">Budget</span>
                <span
                  className={`text-body tabular-nums ${spend.data.over_budget ? "text-error" : ""}`}
                >
                  {money(spend.data.budget_usd)}
                  {spend.data.over_budget ? " · over" : ""}
                </span>
              </div>
            )}
          </section>

          {spend.data.unpriced_calls > 0 && (
            <p className="text-caption text-base-content/60">
              {spend.data.unpriced_calls} call{spend.data.unpriced_calls === 1 ? "" : "s"} ran on a
              model with no known price, so the spend above is a floor rather than the figure. Rows
              affected are marked <span className="text-base-content/40">+</span>.
            </p>
          )}

          <section className="flex flex-col gap-2">
            <h2 className="text-h2">By role and model</h2>
            <div className="overflow-x-auto">
              <table className="text-body w-full">
                <thead className="text-caption text-base-content/50 text-left">
                  <tr>
                    <th className="pb-2 pr-4 font-normal">Name</th>
                    <th className="pb-2 pr-4 text-right font-normal">Calls</th>
                    <th className="pb-2 pr-4 text-right font-normal">Cost</th>
                    <th className="pb-2 pr-4 text-right font-normal">p95 completion</th>
                    <th className="pb-2 text-right font-normal">p95 first token</th>
                  </tr>
                </thead>
                <tbody>
                  <BucketRows buckets={spend.data.by_role} />
                  <BucketRows buckets={spend.data.by_model} />
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}

      <section className="flex flex-col gap-2">
        <h2 className="text-h2">Who is here</h2>
        {/* The count is part of the answer. The list is ordered by cost and it is capped, so
            the learners with no calls — the ones worth noticing — sort last and fall off
            first, and a truncated page otherwise looks exactly like a complete one. */}
        {roster.data && roster.data.learners.length < roster.data.total && (
          <p className="text-caption text-base-content/60">
            Showing the {roster.data.learners.length} costliest of {roster.data.total} accounts.
          </p>
        )}
        {roster.isLoading ? (
          <p className="text-caption text-base-content/50">Loading…</p>
        ) : roster.isError || !roster.data ? (
          <p className="text-body text-error">Could not read the learner list.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="text-body w-full">
              <thead className="text-caption text-base-content/50 text-left">
                <tr>
                  <th className="pb-2 pr-4 font-normal">Learner</th>
                  <th className="pb-2 pr-4 text-right font-normal">Calls</th>
                  <th className="pb-2 pr-4 text-right font-normal">Cost</th>
                  <th className="pb-2 pr-4 text-right font-normal">Last call</th>
                  <th className="pb-2 text-right font-normal" />
                </tr>
              </thead>
              <tbody>
                {roster.data.learners.map((learner) => (
                  <RosterRow key={learner.id} learner={learner} onView={setViewing} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="text-h2">Who has looked at an account</h2>
        <AccessLog />
      </section>

      {viewing && <ViewAsDialog learner={viewing} onClose={() => setViewing(null)} />}
    </div>
  );
}
