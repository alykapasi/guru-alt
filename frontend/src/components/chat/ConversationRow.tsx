import { useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Check, Pencil, Trash2 } from "lucide-react";
import { useDeleteConversation, useRenameConversation } from "../../api/hooks";
import type { components } from "../../api/schema";

type Conversation = components["schemas"]["ConversationRead"];

function label(title: string | null, goal: string | null): string {
  return title ?? goal ?? "New conversation";
}

export function ConversationRow({
  conversation,
  active,
}: {
  conversation: Conversation;
  active: boolean;
}) {
  const navigate = useNavigate();
  const rename = useRenameConversation();
  const del = useDeleteConversation();
  const [editing, setEditing] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function startEditing() {
    setEditing(true);
    // Wait a tick for the input to mount before focusing/selecting.
    requestAnimationFrame(() => {
      inputRef.current?.focus();
      inputRef.current?.select();
    });
  }

  function saveEdit() {
    const value = inputRef.current?.value.trim();
    if (value) {
      rename.mutate({ id: conversation.id, title: value });
    }
    setEditing(false);
  }

  function confirmDelete() {
    del.mutate(conversation.id, {
      onSuccess: () => {
        if (active) navigate("/app/chat");
      },
    });
  }

  if (editing) {
    return (
      <input
        ref={inputRef}
        name="conversation-title"
        defaultValue={conversation.title ?? conversation.goal ?? ""}
        placeholder="Conversation title"
        onBlur={saveEdit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            saveEdit();
          } else if (e.key === "Escape") {
            setEditing(false);
          }
        }}
        className="text-caption bg-base-100 border-primary w-full rounded-field border px-3 py-2 outline-none"
      />
    );
  }

  return (
    <div className="group relative" onMouseLeave={() => setConfirmingDelete(false)}>
      <Link
        to={
          conversation.kind === "session"
            ? `/app/lessons/session/${conversation.id}`
            : `/app/chat/${conversation.id}`
        }
        className={`text-caption flex items-center rounded-field py-2 pr-16 pl-3 transition-colors ${
          active
            ? "bg-primary/10 text-primary"
            : "text-base-content/70 hover:bg-base-200 hover:text-base-content"
        }`}
      >
        <span className="truncate">{label(conversation.title, conversation.goal)}</span>
      </Link>
      <div className="absolute top-1/2 right-1 flex -translate-y-1/2 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
        <button
          onClick={(e) => {
            e.preventDefault();
            startEditing();
          }}
          aria-label="Rename conversation"
          className="hover:bg-base-300 rounded-field p-1.5"
        >
          <Pencil size={13} />
        </button>
        <button
          onClick={(e) => {
            e.preventDefault();
            if (confirmingDelete) {
              confirmDelete();
            } else {
              setConfirmingDelete(true);
            }
          }}
          aria-label={confirmingDelete ? "Confirm delete" : "Delete conversation"}
          className={`rounded-field p-1.5 ${
            confirmingDelete ? "bg-error/15 text-error" : "hover:bg-base-300"
          }`}
        >
          {confirmingDelete ? <Check size={13} /> : <Trash2 size={13} />}
        </button>
      </div>
    </div>
  );
}
