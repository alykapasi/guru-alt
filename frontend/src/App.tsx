import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";
import { PageShell } from "./components/PageShell";
import { RequireLearner } from "./components/RequireLearner";
import { RequireAdmin } from "./components/RequireAdmin";
import { ChatShell } from "./components/chat/ChatShell";
import { SessionShell } from "./components/lessons/SessionShell";
import { Landing } from "./pages/Landing";
import { SignIn } from "./pages/SignIn";
import { ClerkSignUpPanel } from "./auth/ClerkPanels";
import { clerkEnabled } from "./auth/mode";
import { Chat } from "./pages/Chat";
import { ChatIndex } from "./pages/ChatIndex";
import { Lessons } from "./pages/Lessons";
import { Session } from "./pages/Session";
import { Dashboard } from "./pages/Dashboard";
import { Uploads } from "./pages/Uploads";
import { Notes } from "./pages/Notes";
import { NoteView } from "./pages/NoteView";
import { Memory } from "./pages/Memory";
import { SubjectWizard } from "./pages/SubjectWizard";
import { Admin } from "./pages/Admin";

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
        <Route path="/signin/*" element={<SignIn />} />
        {clerkEnabled && <Route path="/sign-up/*" element={<ClerkSignUpPanel />} />}
        {/* Everything under /app needs a learner. The API refuses an unauthenticated request
            regardless; this is what keeps a signed-out browser off a shell of failed calls. */}
        <Route element={<RequireLearner />}>
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
            <Route path="memory" element={<Memory />} />
            <Route path="notes/:topicId" element={<NoteView />} />
            <Route path="subjects/new" element={<SubjectWizard />} />
            {/* The operator's portal (P10). Guarded again inside the shell rather than
                beside it, so an administrator's page keeps the same chrome as every other. */}
            <Route element={<RequireAdmin />}>
              <Route path="admin" element={<Admin />} />
            </Route>
          </Route>
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

export default App;
