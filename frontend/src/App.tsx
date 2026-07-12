import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { PageShell } from "./components/PageShell";
import { Landing } from "./pages/Landing";
import { Chat } from "./pages/Chat";
import { Lessons } from "./pages/Lessons";
import { Dashboard } from "./pages/Dashboard";
import { Uploads } from "./pages/Uploads";

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/app" element={<PageShell />}>
          <Route index element={<Navigate to="chat" replace />} />
          <Route path="chat" element={<Chat />} />
          <Route path="lessons" element={<Lessons />} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="uploads" element={<Uploads />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

export default App;
