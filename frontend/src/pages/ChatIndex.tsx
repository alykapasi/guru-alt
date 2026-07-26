import { Navigate } from "react-router-dom";
import { useConversations } from "../api/hooks";

/** Bare /app/chat — resumes the most recent conversation, or shows an empty prompt if the
 * learner has none yet. Conversations already come back newest-first from the backend. */
export function ChatIndex() {
  const { data: conversations, isLoading } = useConversations();

  if (isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-caption text-base-content/50">Loading…</p>
      </div>
    );
  }

  if (conversations && conversations.length > 0) {
    return <Navigate to={`/app/chat/${conversations[0].id}`} replace />;
  }

  return (
    <div className="flex flex-1 items-center justify-center">
      <p className="text-body text-base-content/50">Start a conversation to begin.</p>
    </div>
  );
}
