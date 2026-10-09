import { useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Archive, ArchiveRestore, Pencil, Trash2 } from "lucide-react";
import { useArchive, useRenameConversation } from "../../api/hooks";
import type { components } from "../../api/schema";
import { RemovalDialog } from "../removal/RemovalDialog";

type Conversation = components["schemas"]["ConversationRead"];

function label(title: string | null, goal: string | null): string {
  return title ?? goal ?? "New conversation";
}

export function ConversationRow({
  conversation,
  active,
  archived = false,
}: {
  conversation: Conversation;
  active: boolean;
  /** A row of the Archived section: Unarchive instead of Archive (S61). */
  archived?: boolean;
}) {
  const navigate = useNavigate();
  const rename = useRenameConversation();
  const archive = useArchive("conversation");
  const [editing, setEditing] = useState(false);
  const [removing, setRemoving] = useState(false);
  const name = label(conversation.title, conversation.goal);
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

  function deleted() {
    setRemoving(false);
    if (active) navigate("/app/chat");
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
    <div className="group relative">
      <Link
        to={
          conversation.kind === "session"
            ? `/app/lessons/session/${conversation.id}`
            : `/app/chat/${conversation.id}`
        }
        aria-current={active ? "page" : undefined}
        className={`text-caption flex items-center rounded-field py-2 pr-16 pl-3 transition-colors ${
          active
            ? "bg-primary/10 text-primary"
            : "text-base-content/70 hover:bg-base-200 hover:text-base-content"
        }`}
      >
        <span className="truncate">{name}</span>
      </Link>
      <div className="absolute top-1/2 right-1 flex -translate-y-1/2 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 max-lg:opacity-100 pointer-coarse:opacity-100">
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
            archive.mutate({ id: conversation.id, archived: !archived });
          }}
          disabled={archive.isPending}
          aria-label={archived ? "Unarchive conversation" : "Archive conversation"}
          className="hover:bg-base-300 rounded-field p-1.5"
        >
          {archived ? <ArchiveRestore size={13} /> : <Archive size={13} />}
        </button>
        <button
          onClick={(e) => {
            e.preventDefault();
            setRemoving(true);
          }}
          aria-label="Delete conversation"
          className="hover:bg-base-300 rounded-field p-1.5"
        >
          <Trash2 size={13} />
        </button>
      </div>
      {removing && (
        <RemovalDialog
          kind="conversation"
          id={conversation.id}
          name={name}
          open
          onClose={() => setRemoving(false)}
          onDeleted={deleted}
        />
      )}
    </div>
  );
}
