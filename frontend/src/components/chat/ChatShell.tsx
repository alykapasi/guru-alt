import { Outlet } from "react-router-dom";
import { NavBar } from "../NavBar";
import { ConversationSidebar } from "./ConversationSidebar";

/** Full-height two-pane layout for the chat route — breaks out of PageShell's centered
 * max-w-[1280px] content column, since a sidebar + transcript needs the full viewport
 * width/height below the nav, not a centered text column. */
export function ChatShell() {
  return (
    <div className="bg-base-100 flex h-svh flex-col">
      <NavBar />
      <div className="flex min-h-0 flex-1">
        <ConversationSidebar />
        <Outlet />
      </div>
    </div>
  );
}
