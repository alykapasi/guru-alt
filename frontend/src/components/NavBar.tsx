import { NavLink, useNavigate } from "react-router-dom";
import {
  BookOpen,
  LayoutDashboard,
  LogOut,
  MessageSquare,
  NotebookText,
  Upload,
} from "lucide-react";
import { Logo } from "./Logo";
import { ThemeToggle } from "./ThemeToggle";
import { useCurrentLearner, useLogout } from "../api/auth";

const LINKS = [
  { to: "/app/chat", label: "Chat", icon: MessageSquare },
  { to: "/app/lessons", label: "Lessons", icon: NotebookText },
  { to: "/app/notes", label: "Notes", icon: BookOpen },
  { to: "/app/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { to: "/app/uploads", label: "Uploads", icon: Upload },
];

export function NavBar() {
  const navigate = useNavigate();
  const { data: learner } = useCurrentLearner();
  const logout = useLogout();

  async function signOut() {
    await logout.mutateAsync();
    navigate("/signin", { replace: true });
  }

  return (
    <header className="border-base-300 bg-base-100 border-b">
      <div className="mx-auto flex h-16 max-w-[1280px] items-center justify-between px-6">
        <NavLink to="/" className="shrink-0">
          <Logo />
        </NavLink>
        <nav className="flex items-center gap-1">
          {LINKS.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `flex items-center gap-2 rounded-field px-3 py-2 text-caption transition-colors ${
                  isActive
                    ? "bg-primary/10 text-primary"
                    : "text-base-content/70 hover:bg-base-200 hover:text-base-content"
                }`
              }
            >
              <Icon size={16} />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="flex items-center gap-2">
          {learner && (
            <span className="text-caption text-base-content/60 hidden sm:inline">
              {learner.display_name || learner.handle}
            </span>
          )}
          <ThemeToggle />
          <button
            type="button"
            onClick={signOut}
            disabled={logout.isPending}
            aria-label="Sign out"
            title="Sign out"
            className="text-base-content/70 hover:bg-base-200 hover:text-base-content rounded-field p-2 transition-colors"
          >
            <LogOut size={16} />
          </button>
        </div>
      </div>
    </header>
  );
}
