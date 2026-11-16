import { useRef, useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import {
  BookOpen,
  Brain,
  ChevronDown,
  Gauge,
  LayoutDashboard,
  LogOut,
  Menu,
  MessageSquare,
  NotebookText,
  Upload,
  UserRound,
} from "lucide-react";
import { Logo } from "./Logo";
import { ThemeToggle } from "./ThemeToggle";
import { useCurrentLearner } from "../api/auth";
import { ClerkUserButton } from "../auth/ClerkPanels";
import { clerkEnabled } from "../auth/mode";
import { useSignOutEverywhere } from "../auth/session";

/** Where a learner works, in the order they use it. */
const LINKS = [
  { to: "/app/lessons", label: "Lessons", icon: NotebookText },
  { to: "/app/chat", label: "Chat", icon: MessageSquare },
  { to: "/app/notes", label: "Notes", icon: BookOpen },
  { to: "/app/uploads", label: "Uploads", icon: Upload },
  { to: "/app/dashboard", label: "Dashboard", icon: LayoutDashboard },
];

/** About the learner rather than their learning — under their name, out of the main bar. */
const ACCOUNT_LINKS = [
  { to: "/app/memory", label: "Memory", icon: Brain },
  { to: "/app/account", label: "Account", icon: UserRound },
];

/** Offered only to an administrator (P10). Not a security decision — the API refuses a
 * non-administrator whatever is rendered — but showing everybody a link that answers 403 is
 * how a product teaches people it is broken. */
const ADMIN_LINK = { to: "/app/admin", label: "Admin", icon: Gauge };

const linkClass = ({ isActive }: { isActive: boolean }) =>
  `flex items-center gap-2 rounded-field px-3 py-2 text-caption transition-colors ${
    isActive
      ? "bg-primary/10 text-primary"
      : "text-base-content/70 hover:bg-base-200 hover:text-base-content"
  }`;

export function NavBar() {
  const navigate = useNavigate();
  const { data: learner } = useCurrentLearner();
  const signOutEverywhere = useSignOutEverywhere();
  const [busy, setBusy] = useState(false);
  const menuRef = useRef<HTMLDetailsElement>(null);
  const accountRef = useRef<HTMLDetailsElement>(null);
  const accountLinks = [...ACCOUNT_LINKS, ...(learner?.is_admin ? [ADMIN_LINK] : [])];

  async function signOut() {
    setBusy(true);
    try {
      await signOutEverywhere();
      navigate("/signin", { replace: true });
    } finally {
      setBusy(false);
    }
  }

  return (
    <header className="border-base-300 bg-base-100 border-b">
      <div className="mx-auto flex h-16 max-w-[1280px] items-center justify-between gap-2 px-4 sm:px-6">
        <NavLink to="/" className="shrink-0">
          <Logo />
        </NavLink>
        {/* Below `sm` the links do not fit beside the logo and account controls (S53); a
            <details> disclosure opens and closes from the keyboard with no script of ours. */}
        <details ref={menuRef} aria-label="Menu" className="dropdown sm:hidden">
          <summary className="btn btn-ghost btn-sm" aria-label="Open menu">
            <Menu size={18} />
          </summary>
          <ul className="dropdown-content menu bg-base-100 rounded-box border-base-300 z-50 mt-2 w-52 border p-2 shadow">
            {LINKS.map(({ to, label, icon: Icon }) => (
              <li key={to}>
                <NavLink
                  to={to}
                  className={linkClass}
                  onClick={() => menuRef.current?.removeAttribute("open")}
                >
                  <Icon size={16} />
                  {label}
                </NavLink>
              </li>
            ))}
          </ul>
        </details>
        <nav aria-label="Main" className="hidden items-center gap-1 sm:flex">
          {LINKS.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} className={linkClass}>
              <Icon size={16} />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="flex items-center gap-2">
          <ThemeToggle />
          <details ref={accountRef} aria-label="Account menu" className="dropdown dropdown-end">
            <summary className="text-base-content/70 hover:bg-base-200 hover:text-base-content rounded-field flex cursor-pointer list-none items-center gap-1 px-2 py-2 text-caption transition-colors">
              <UserRound size={16} aria-hidden />
              <span className="sr-only sm:hidden">Account</span>
              <span className="hidden max-w-40 truncate sm:inline">
                {learner ? learner.display_name || learner.handle : "Account"}
              </span>
              <ChevronDown size={14} aria-hidden />
            </summary>
            <ul className="dropdown-content menu bg-base-100 rounded-box border-base-300 z-50 mt-2 w-52 border p-2 shadow">
              {accountLinks.map(({ to, label, icon: Icon }) => (
                <li key={to}>
                  <NavLink
                    to={to}
                    className={linkClass}
                    onClick={() => accountRef.current?.removeAttribute("open")}
                  >
                    <Icon size={16} />
                    {label}
                  </NavLink>
                </li>
              ))}
              {/* Clerk's own menu carries sign-out and account management together, so Guru's
                  button would offer the same action twice and disagree about what it does.
                  `ClerkSessionWatcher` ends Guru's session when Clerk's ends (S21). */}
              {!clerkEnabled && (
                <li>
                  <button
                    type="button"
                    onClick={signOut}
                    disabled={busy}
                    className="text-base-content/70 hover:bg-base-200 hover:text-base-content rounded-field flex items-center gap-2 px-3 py-2 text-caption"
                  >
                    <LogOut size={16} />
                    Sign out
                  </button>
                </li>
              )}
            </ul>
          </details>
          {clerkEnabled && <ClerkUserButton />}
        </div>
      </div>
    </header>
  );
}
