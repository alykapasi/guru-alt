import { useState } from "react";
import { useMatch } from "react-router-dom";
import { Plus } from "lucide-react";
import { useArchivedConversations, useConversations } from "../../api/hooks";
import { ConversationRow } from "./ConversationRow";
import { NewChatModal } from "./NewChatModal";
import { LoadMore } from "../LoadMore";

export function ConversationSidebar() {
  const {
    data: conversations,
    isLoading,
    hasNextPage,
    fetchNextPage,
    isFetchingNextPage,
  } = useConversations();
  const archivedQuery = useArchivedConversations();
  const archived = archivedQuery.data;
  const activeId = useMatch("/app/chat/:conversationId")?.params.conversationId;
  const [modalOpen, setModalOpen] = useState(false);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="border-base-300 border-b p-4">
        <button onClick={() => setModalOpen(true)} className="btn btn-primary btn-sm w-full">
          <Plus size={16} />
          New chat
        </button>
        <NewChatModal open={modalOpen} onClose={() => setModalOpen(false)} />
      </div>
      <nav className="flex-1 overflow-y-auto p-2">
        {isLoading && <p className="text-caption text-base-content/70 px-3 py-2">Loading…</p>}
        {conversations?.length === 0 && (
          <p className="text-caption text-base-content/70 px-3 py-2">
            No conversations yet — start one above.
          </p>
        )}
        {conversations?.map((c) => (
          <ConversationRow key={c.id} conversation={c} active={c.id === activeId} />
        ))}
        <LoadMore
          hasNextPage={hasNextPage}
          isFetchingNextPage={isFetchingNextPage}
          fetchNextPage={fetchNextPage}
        />
        {archived && archived.length > 0 && (
          <details className="mt-3">
            <summary className="text-caption text-base-content/70 cursor-pointer px-3 py-1">
              Archived ({archived.length}
              {archivedQuery.hasNextPage ? "+" : ""})
            </summary>
            {archived.map((c) => (
              <ConversationRow key={c.id} conversation={c} active={c.id === activeId} archived />
            ))}
            <LoadMore
              hasNextPage={archivedQuery.hasNextPage}
              isFetchingNextPage={archivedQuery.isFetchingNextPage}
              fetchNextPage={archivedQuery.fetchNextPage}
            />
          </details>
        )}
      </nav>
    </div>
  );
}
