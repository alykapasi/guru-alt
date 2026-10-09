import { useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { PanelLeft } from "lucide-react";
import { ImpersonationBanner } from "../ImpersonationBanner";
import { NavBar } from "../NavBar";
import { SkipLink } from "../layout/SkipLink";
import { SidePanel } from "../layout/SidePanel";
import { useMediaQuery, WIDE_QUERY } from "../../hooks/useMediaQuery";
import { ConversationSidebar } from "./ConversationSidebar";

/** Full-height two-pane layout for the chat route — breaks out of PageShell's centered
 * max-w-[1280px] content column, since a sidebar + transcript needs the full viewport
 * width/height below the nav, not a centered text column. Below the wide breakpoint the
 * conversation list is a drawer, so the transcript keeps the whole width (S53). */
export function ChatShell() {
  const wide = useMediaQuery(WIDE_QUERY);
  const { pathname } = useLocation();
  // Keyed on the path it was opened at: choosing a conversation, or starting one, is a
  // navigation, and the drawer has done its job — no effect needed to close it.
  const [drawer, setDrawer] = useState<{ open: boolean; at: string }>({
    open: false,
    at: pathname,
  });
  const drawerOpen = drawer.open && drawer.at === pathname;

  return (
    <div className="bg-base-100 flex h-svh flex-col">
      <SkipLink />
      {/* This route is the likeliest reason to be viewing an account at all, so it is the
          likeliest place to forget that you are (P10). */}
      <ImpersonationBanner />
      <NavBar />
      {!wide && (
        <div className="border-base-300 flex items-center border-b px-4 py-2">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => setDrawer({ open: true, at: pathname })}
          >
            <PanelLeft size={16} />
            Conversations
          </button>
        </div>
      )}
      <div className="flex min-h-0 flex-1">
        <SidePanel
          open={wide || drawerOpen}
          onClose={() => setDrawer({ open: false, at: pathname })}
          side="left"
          label="Conversations"
          width="w-72"
        >
          <ConversationSidebar />
        </SidePanel>
        <Outlet />
      </div>
    </div>
  );
}
