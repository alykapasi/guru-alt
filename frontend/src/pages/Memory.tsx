import { useState } from "react";
import { Link } from "react-router-dom";
import { Brain, Check, Pencil, Trash2, X } from "lucide-react";
import {
  useCorrectMemory,
  useForgetAllMemory,
  useForgetMemory,
  useMemorySetting,
  useUndoReplacement,
  useForgetOrigin,
  useMemories,
} from "../api/hooks";
import type { components } from "../api/schema";

type Memory = components["schemas"]["MemoryRead"];

/** What the system believes about the learner, and the means to say otherwise (S16).
 *
 * All three modes have shared one picture of the learner since S16 landed, and the learner
 * could see none of it. That matters most at exactly the moment the picture is wrong: a
 * mistaken fact about somebody is carried into every conversation they have, invisibly, and
 * the only thing they could previously do about it was nothing.
 *
 * Correcting is offered ahead of forgetting on purpose. Most of what goes wrong with an
 * extracted memory is that it is nearly right, and erasing it throws away the true part while
 * leaving the extractor free to derive the same mistake again from the same history. */

/** Where a memory was learned (S42), and the means to forget everything learned there — which
 * still works once that conversation is deleted, because the origin outlives it. */
function Origin({ memory }: { memory: Memory }) {
  const forgetOrigin = useForgetOrigin();
  const [confirming, setConfirming] = useState(false);
  const origin = memory.origin_conversation_id;
  if (!origin) return null;
  return (
    <div className="text-caption text-base-content/50 flex items-center gap-2 pl-27">
      <span>
        {memory.origin_live
          ? `From: ${memory.origin_title ?? "an untitled conversation"}`
          : "From a deleted conversation"}
      </span>
      {confirming ? (
        <>
          <button
            type="button"
            className="btn btn-error btn-xs"
            disabled={forgetOrigin.isPending}
            onClick={() => forgetOrigin.mutate(origin, { onSuccess: () => setConfirming(false) })}
          >
            Yes, forget them
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            onClick={() => setConfirming(false)}
          >
            Keep
          </button>
        </>
      ) : (
        <button type="button" className="btn btn-ghost btn-xs" onClick={() => setConfirming(true)}>
          Forget all from this conversation
        </button>
      )}
    </div>
  );
}

const KIND_COPY: Record<string, string> = {
  fact: "Fact",
  preference: "Preference",
  summary: "Summary",
};

function Row({ memory }: { memory: Memory }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(memory.content);
  const correct = useCorrectMemory();
  const forget = useForgetMemory();
  const undo = useUndoReplacement();

  const save = () => {
    const next = draft.trim();
    if (!next || next === memory.content.trim()) {
      setEditing(false);
      return;
    }
    correct.mutate({ id: memory.id, content: next }, { onSuccess: () => setEditing(false) });
  };

  return (
    <li className="border-base-300 flex flex-col gap-2 border-b py-3">
      <div className="flex items-start gap-3">
        <span className="text-caption text-base-content/50 w-24 shrink-0 pt-1">
          {KIND_COPY[memory.kind] ?? memory.kind}
        </span>
        {editing ? (
          <textarea
            className="textarea textarea-bordered text-body min-h-20 flex-1"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            aria-label="What is actually true"
          />
        ) : (
          <p className="text-body text-base-content/90 flex-1">{memory.content}</p>
        )}
        <div className="flex shrink-0 gap-1">
          {editing ? (
            <>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                onClick={save}
                disabled={correct.isPending}
                aria-label="Save correction"
              >
                <Check size={14} />
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                onClick={() => {
                  setDraft(memory.content);
                  setEditing(false);
                }}
                aria-label="Cancel"
              >
                <X size={14} />
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                onClick={() => setEditing(true)}
                aria-label={`Correct: ${memory.content}`}
              >
                <Pencil size={14} />
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-xs text-error"
                onClick={() => forget.mutate(memory.id)}
                disabled={forget.isPending}
                aria-label={`Forget: ${memory.content}`}
              >
                <Trash2 size={14} />
              </button>
            </>
          )}
        </div>
      </div>
      {memory.replaced && (
        <p className="text-caption text-base-content/60 pl-24">
          Replaced: {memory.replaced.content}{" "}
          <button
            type="button"
            className="link"
            disabled={undo.isPending}
            onClick={() => undo.mutate(memory.id)}
            aria-label={`Undo replacement of: ${memory.replaced.content}`}
          >
            Undo
          </button>
        </p>
      )}
      {undo.isError && (
        <p className="text-caption text-error pl-24">
          That can&apos;t be undone any more — the earlier memory changed since.
        </p>
      )}
      <Origin memory={memory} />
      {correct.isError && (
        <p className="text-caption text-error pl-24">
          That correction didn&apos;t save. Try again.
        </p>
      )}
    </li>
  );
}

export function Memory() {
  const { data, isLoading } = useMemories();
  const { data: setting } = useMemorySetting();
  const forgetAll = useForgetAllMemory();
  const [confirming, setConfirming] = useState(false);

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 px-6 py-8">
      <header className="flex flex-col gap-2">
        <h1 className="text-h1 flex items-center gap-2">
          <Brain size={20} className="text-primary" aria-hidden />
          What Guru remembers about you
        </h1>
        <p className="text-body text-base-content/70">
          These are carried into every conversation, whichever mode you are in. If one is wrong,
          correcting it works better than deleting it — Guru keeps the correction and stops drawing
          the old conclusion from the same conversation.
        </p>
      </header>

      {setting?.remember === false && (
        <p className="alert alert-info text-body">
          Memory is paused — Guru isn&apos;t learning anything new from your conversations. Turn it
          back on in{" "}
          <Link to="/account" className="link">
            Account
          </Link>
          .
        </p>
      )}

      {isLoading ? (
        <p className="text-caption text-base-content/50">Loading…</p>
      ) : !data?.length ? (
        <p className="text-body text-base-content/60">
          Nothing yet. Guru records something here when a conversation says something durable about
          how you learn or what you already know.
        </p>
      ) : (
        <>
          <ul className="border-base-300 flex flex-col border-t">
            {data.map((memory) => (
              <Row key={memory.id} memory={memory} />
            ))}
          </ul>
          <div className="flex items-center gap-3">
            {confirming ? (
              <>
                <button
                  type="button"
                  className="btn btn-error btn-sm"
                  onClick={() =>
                    forgetAll.mutate(undefined, { onSuccess: () => setConfirming(false) })
                  }
                  disabled={forgetAll.isPending}
                >
                  Yes, forget everything
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => setConfirming(false)}
                >
                  Keep it
                </button>
              </>
            ) : (
              <button
                type="button"
                className="btn btn-outline btn-sm"
                onClick={() => setConfirming(true)}
              >
                Forget everything
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
