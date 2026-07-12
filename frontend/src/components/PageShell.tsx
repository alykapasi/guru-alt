import { Outlet } from "react-router-dom";
import { NavBar } from "./NavBar";

/** Layout for every route under /app: top nav + content area. */
export function PageShell() {
  return (
    <div className="bg-base-100 min-h-svh">
      <NavBar />
      <main className="mx-auto max-w-[1280px] px-6 py-12">
        <Outlet />
      </main>
    </div>
  );
}
