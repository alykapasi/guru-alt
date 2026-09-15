import { useState } from "react";
import { Gauge } from "lucide-react";
import { useLearnerRoster, useSpend } from "../api/admin";
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
  const minutes = Math.round((Date.now() - new Date(iso + "Z").getTime()) / 60_000);
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

function RosterRow({ learner }: { learner: LearnerUsage }) {
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
      <td className="py-2 text-right tabular-nums">{ago(learner.last_call_at)}</td>
    </tr>
  );
}

export function Admin() {
  const [hours, setHours] = useState(24);
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
                  <th className="pb-2 text-right font-normal">Last call</th>
                </tr>
              </thead>
              <tbody>
                {roster.data.learners.map((learner) => (
                  <RosterRow key={learner.id} learner={learner} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
