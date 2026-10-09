import { useEffect, useRef, type ReactNode } from "react";
import { X } from "lucide-react";
import { useMediaQuery, WIDE_QUERY } from "../../hooks/useMediaQuery";

/** A panel beside the content on a wide screen and a sheet over it on a narrow one (S53).
 *
 * The narrow form is a native modal dialog on purpose: the browser then traps focus inside
 * it, closes it on Escape, returns focus to whatever opened it, and hides the page behind it
 * from assistive technology — four things a hand-rolled drawer gets wrong one at a time.
 * Its children stay mounted while it is closed, so a half-revealed flashcard or a scrolled
 * list is where the learner left it when the sheet opens again. */
export function SidePanel({
  open,
  onClose,
  side,
  label,
  width,
  showClose = true,
  children,
}: {
  open: boolean;
  onClose: () => void;
  side: "left" | "right";
  label: string;
  /** Tailwind width class for the wide column, e.g. "w-80". */
  width: string;
  /** False when the content carries its own close control. */
  showClose?: boolean;
  children: ReactNode;
}) {
  const wide = useMediaQuery(WIDE_QUERY);
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    else if (!open && dialog.open) dialog.close();
  }, [open, wide]);

  if (wide) {
    if (!open) return null;
    const edge = side === "left" ? "border-r" : "border-l";
    return (
      <aside
        aria-label={label}
        className={`border-base-300 bg-base-100 flex ${width} min-h-0 shrink-0 flex-col ${edge}`}
      >
        {children}
      </aside>
    );
  }

  const placement = side === "left" ? "mr-auto" : "ml-auto";
  return (
    <dialog
      ref={dialogRef}
      aria-label={label}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClose={() => {
        if (open) onClose();
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      className={`bg-base-100 m-0 ${placement} h-svh max-h-svh w-[85vw] max-w-sm p-0 backdrop:bg-black/40`}
    >
      <div className="flex h-full min-h-0 flex-col">
        {showClose && (
          <div className="border-base-300 flex justify-end border-b p-2">
            <button
              type="button"
              onClick={onClose}
              aria-label={`Close ${label}`}
              className="hover:bg-base-200 rounded-field p-1.5"
            >
              <X size={16} />
            </button>
          </div>
        )}
        {children}
      </div>
    </dialog>
  );
}
