import { useState } from "react";
import {
  useDraw,
  useEvalAreas,
  useNextExample,
  useSaveLabel,
  type BlindExample,
  type EvalArea,
  type RevealedExample,
} from "../../api/evalExamples";

/** Label the founder's own examples, blind (S59).
 *
 * The model's decision is not fetched until a label is saved — the server serves an unlabelled
 * example without it — so what gets measured is agreement, not deference. */

const TITLES: Record<string, string> = {
  grading: "Grading",
  diagnosis: "Diagnosis",
  intent: "Intent",
  source_support: "Source support",
};

const FIELDS: Record<string, string> = {
  stem: "Question",
  answer: "Answer",
  criteria: "Criteria",
  component: "Component",
  message: "Reply",
  question: "Learner asked",
  reply: "Tutor replied",
  offered: "Passages offered",
};

function Snapshot({ snapshot }: { snapshot: Record<string, unknown> }) {
  const passages = Array.isArray(snapshot.passages)
    ? (snapshot.passages as { marker?: string; text: string }[])
    : [];
  return (
    <dl className="flex flex-col gap-2">
      {Object.entries(FIELDS).map(([key, title]) =>
        snapshot[key] === undefined || snapshot[key] === null ? null : (
          <div key={key}>
            <dt className="text-caption text-base-content/70">{title}</dt>
            <dd className="text-body whitespace-pre-wrap">
              {typeof snapshot[key] === "string"
                ? (snapshot[key] as string)
                : JSON.stringify(snapshot[key])}
            </dd>
          </div>
        ),
      )}
      {passages.length > 0 && (
        <div>
          <dt className="text-caption text-base-content/70">Passages cited</dt>
          {passages.map((p, i) => (
            <dd key={i} className="text-body whitespace-pre-wrap">
              {p.marker ? `[${p.marker}] ` : ""}
              {p.text}
            </dd>
          ))}
        </div>
      )}
    </dl>
  );
}

function Labeller({
  area,
  example,
  onNext,
  onSkip,
}: {
  area: EvalArea;
  example: BlindExample;
  onNext: () => void;
  onSkip: () => void;
}) {
  const [choice, setChoice] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [revealed, setRevealed] = useState<RevealedExample | null>(null);
  const save = useSaveLabel();

  const decided = revealed ? String(revealed.decision[area.label_key] ?? "—") : null;

  return (
    <div className="flex flex-col gap-3">
      <Snapshot snapshot={example.snapshot as Record<string, unknown>} />
      <fieldset role="radiogroup" aria-label="Your label" className="flex flex-wrap gap-3">
        {area.values.map((value) => (
          <label key={value} className="flex items-center gap-1 text-body">
            <input
              type="radio"
              name={`label-${example.id}`}
              value={value}
              checked={choice === value}
              disabled={revealed !== null}
              onChange={() => setChoice(value)}
            />
            {value}
          </label>
        ))}
      </fieldset>
      <textarea
        aria-label="Note"
        className="textarea textarea-bordered w-full"
        value={note}
        disabled={revealed !== null}
        onChange={(e) => setNote(e.target.value)}
      />
      {save.error && <p className="text-caption text-error">{save.error.message}</p>}
      {revealed ? (
        <div className="flex items-center gap-3">
          <p className="text-body">
            {`Model decided: ${decided}${decided === choice ? " — you agreed." : " — you differed."}`}
          </p>
          <button type="button" className="btn btn-sm" onClick={onNext}>
            Next
          </button>
        </div>
      ) : (
        <div className="flex gap-2">
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={choice === null || save.isPending}
            onClick={() =>
              choice &&
              save.mutate(
                { id: example.id, label: { [area.label_key]: choice }, note },
                { onSuccess: setRevealed },
              )
            }
          >
            Save label
          </button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onSkip}>
            Skip
          </button>
        </div>
      )}
    </div>
  );
}

function AreaPanel({ area }: { area: EvalArea }) {
  const [skip, setSkip] = useState<string[]>([]);
  const next = useNextExample(area.area, skip);
  const draw = useDraw(area.area);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-3">
        <button
          type="button"
          className="btn btn-sm"
          disabled={draw.isPending}
          onClick={() => draw.mutate()}
        >
          Draw 10
        </button>
        {draw.data !== undefined && (
          <span className="text-caption text-base-content/70">
            {draw.data === 0 ? "No new examples to draw." : `Drew ${draw.data}.`}
          </span>
        )}
      </div>
      {next.isLoading ? (
        <p className="text-caption text-base-content/70">Loading…</p>
      ) : next.isError ? (
        <p className="text-body text-error">{next.error.message}</p>
      ) : next.data ? (
        <Labeller
          key={next.data.id}
          area={area}
          example={next.data}
          onNext={() => next.refetch()}
          onSkip={() => setSkip([...skip, next.data!.id])}
        />
      ) : (
        <p className="text-body text-base-content/70">Nothing to label — draw some examples.</p>
      )}
    </div>
  );
}

export function ReviewExamples() {
  const areas = useEvalAreas();
  const [open, setOpen] = useState<string | null>(null);

  if (areas.isLoading) return <p className="text-caption text-base-content/70">Loading…</p>;
  if (areas.isError || !areas.data)
    return <p className="text-body text-error">Could not read the evaluation areas.</p>;

  const current = areas.data.find((a) => a.area === open) ?? null;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-2">
        {areas.data.map((a) => (
          <button
            key={a.area}
            type="button"
            aria-pressed={open === a.area}
            className="btn btn-sm flex-col items-start"
            onClick={() => setOpen(a.area)}
          >
            <span>{TITLES[a.area] ?? a.area}</span>
            <span className="text-caption">
              {a.unlabelled} to label · {a.labelled} labelled · {a.available} available
            </span>
          </button>
        ))}
      </div>
      {current && <AreaPanel key={current.area} area={current} />}
    </div>
  );
}
