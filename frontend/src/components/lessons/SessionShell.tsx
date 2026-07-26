import { Outlet } from "react-router-dom";
import { NavBar } from "../NavBar";

/** Full-height layout for a guided-practice session — like ChatShell, breaks out of PageShell's
 * centered column so the transcript + item side panel can use the full viewport, but with no
 * conversation sidebar (a session is a single focused run, not something to switch between). */
export function SessionShell() {
  return (
    <div className="bg-base-100 flex h-svh flex-col">
      <NavBar />
      <div className="flex min-h-0 flex-1">
        <Outlet />
      </div>
    </div>
  );
}
