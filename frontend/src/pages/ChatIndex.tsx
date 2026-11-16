import { Navigate } from "react-router-dom";
import { useConversations } from "../api/hooks";

/** Bare /app/chat — resumes the most recent chat, or shows an empty prompt if the learner has
 * none yet. Conversations come back newest-first; a practice session is the Lessons page's, so
 * the Chat tab never opens one. */
export function ChatIndex() {
  const { data: conversations, isLoading } = useConversations();

  if (isLoading) {
    return (
      <main
        id="main"
        tabIndex={-1}
        className="outline-none flex flex-1 items-center justify-center"
      >
        <p className="text-caption text-base-content/70">Loading…</p>
      </main>
    );
  }

  const latest = conversations?.find((c) => c.kind === "chat");
  if (latest) {
    return <Navigate to={`/app/chat/${latest.id}`} replace />;
  }

  return (
    <main id="main" tabIndex={-1} className="outline-none flex flex-1 items-center justify-center">
      <p className="text-body text-base-content/70">Start a conversation to begin.</p>
    </main>
  );
}
