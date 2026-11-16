import { lazy } from "react";
import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";
import { PageShell } from "./components/PageShell";
import { RequireLearner } from "./components/RequireLearner";
import { RequireAdmin } from "./components/RequireAdmin";
import { ChatShell } from "./components/chat/ChatShell";
import { SessionShell } from "./components/lessons/SessionShell";
import { Landing } from "./pages/Landing";
import { SignIn } from "./pages/SignIn";
import { SignUp } from "./pages/SignUp";
import { clerkEnabled } from "./auth/mode";
import { HOME } from "./lib/home";

/** Everything behind sign-in loads on demand, a page at a time, so the landing and sign-in
 * pages do not carry the app — and a learner never downloads the admin portal. The shells
 * hold the loading boundary (`LazyOutlet`). */
const page = <K extends string>(load: () => Promise<Record<K, React.ComponentType>>, name: K) =>
  lazy(() => load().then((m) => ({ default: m[name] })));
const Chat = page(() => import("./pages/Chat"), "Chat");
const ChatIndex = page(() => import("./pages/ChatIndex"), "ChatIndex");
const Lessons = page(() => import("./pages/Lessons"), "Lessons");
const Session = page(() => import("./pages/Session"), "Session");
const Dashboard = page(() => import("./pages/Dashboard"), "Dashboard");
const Uploads = page(() => import("./pages/Uploads"), "Uploads");
const Notes = page(() => import("./pages/Notes"), "Notes");
const NoteView = page(() => import("./pages/NoteView"), "NoteView");
const Memory = page(() => import("./pages/Memory"), "Memory");
const Account = page(() => import("./pages/Account"), "Account");
const SubjectWizard = page(() => import("./pages/SubjectWizard"), "SubjectWizard");
const Admin = page(() => import("./pages/Admin"), "Admin");

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
        {/* Only with a provider: without one there is nothing to register against, and a
            route that renders an explanation is a route people can still link to. */}
        {clerkEnabled && <Route path="/sign-up/*" element={<SignUp />} />}
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
            <Route index element={<Navigate to={HOME} replace />} />
            <Route path="lessons" element={<Lessons />} />
            <Route path="dashboard" element={<Dashboard />} />
            <Route path="uploads" element={<Uploads />} />
            <Route path="notes" element={<Notes />} />
            <Route path="memory" element={<Memory />} />
            <Route path="account" element={<Account />} />
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
