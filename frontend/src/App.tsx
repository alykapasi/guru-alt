import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";
import { PageShell } from "./components/PageShell";
import { ChatShell } from "./components/chat/ChatShell";
import { SessionShell } from "./components/lessons/SessionShell";
import { Landing } from "./pages/Landing";
import { Chat } from "./pages/Chat";
import { ChatIndex } from "./pages/ChatIndex";
import { Lessons } from "./pages/Lessons";
import { Session } from "./pages/Session";
import { Dashboard } from "./pages/Dashboard";
import { Uploads } from "./pages/Uploads";
import { Notes } from "./pages/Notes";
import { NoteView } from "./pages/NoteView";
import { SubjectWizard } from "./pages/SubjectWizard";

/** Keys Chat on conversationId so switching conversations remounts it fresh — its local
 * in-flight-turn state (see useChatConversation) must not carry over between conversations. */
function ChatRoute() {
  const { conversationId } = useParams();
  return <Chat key={conversationId} />;
}

/** Same remount-on-id rationale as ChatRoute — a session's local turn state must not leak
 * across a navigation from one practice session's conversation to another's. */
function SessionRoute() {
  const { conversationId } = useParams();
  return <Session key={conversationId} />;
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/app/chat" element={<ChatShell />}>
          <Route index element={<ChatIndex />} />
          <Route path=":conversationId" element={<ChatRoute />} />
        </Route>
        <Route path="/app/lessons/session" element={<SessionShell />}>
          <Route path=":conversationId" element={<SessionRoute />} />
        </Route>
        <Route path="/app" element={<PageShell />}>
          <Route index element={<Navigate to="chat" replace />} />
          <Route path="lessons" element={<Lessons />} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="uploads" element={<Uploads />} />
          <Route path="notes" element={<Notes />} />
          <Route path="notes/:topicId" element={<NoteView />} />
          <Route path="subjects/new" element={<SubjectWizard />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

export default App;
