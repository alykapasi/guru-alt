/** A page with nothing in it yet: the same heading every page has, and one sentence on what
 * fills it and how. */
export function EmptyPage({ title, description }: { title: string; description: string }) {
  return (
    <div className="flex flex-col gap-2">
      <h1 className="text-h1">{title}</h1>
      <p className="text-body text-base-content/70 max-w-xl">{description}</p>
    </div>
  );
}
