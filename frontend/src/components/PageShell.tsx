import { Outlet } from "react-router-dom";
import { ImpersonationBanner } from "./ImpersonationBanner";
import { NavBar } from "./NavBar";

/** Layout for every route under /app: top nav + content area. */
export function PageShell() {
  return (
    <div className="bg-base-100 min-h-svh">
      {/* Above the nav rather than inside the content: an administrator who has forgotten
          whose account they are reading is exactly the person who will not notice a notice
          that scrolls away (P10). */}
      <ImpersonationBanner />
      <NavBar />
      <main className="mx-auto max-w-[1280px] px-6 py-12">
        <Outlet />
      </main>
    </div>
  );
}
