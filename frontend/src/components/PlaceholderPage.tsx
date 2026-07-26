import type { LucideIcon } from "lucide-react";

/** Shared shape for the four not-yet-built app screens — honest about being a placeholder
 * without looking unfinished: a real icon, a real heading, one concrete sentence about what
 * lands here next (not lorem ipsum). */
export function PlaceholderPage({
  icon: Icon,
  title,
  description,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
}) {
  return (
    <div className="border-base-300 flex flex-col items-start gap-4 border-t py-16">
      <div className="bg-primary/10 text-primary rounded-box flex size-12 items-center justify-center">
        <Icon size={22} />
      </div>
      <h1 className="text-h1">{title}</h1>
      <p className="text-body text-base-content/70 max-w-md">{description}</p>
    </div>
  );
}
