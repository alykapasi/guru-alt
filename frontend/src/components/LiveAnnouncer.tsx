/** One polite region per page, always mounted: a region inserted together with its text is
 * not reliably announced, one whose text changes is. */
export function LiveAnnouncer({ message }: { message: string }) {
  return (
    <div role="status" aria-live="polite" className="sr-only">
      {message}
    </div>
  );
}
