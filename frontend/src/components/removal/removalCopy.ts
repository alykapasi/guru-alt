/** Plain-language lines for what a removal keeps or forgets (S61, V11). */
type Copy = Record<string, (n: number) => string>;

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

export const KEPT_COPY: Copy = {
  lessons: (n) => `${plural(n, "lesson", "lessons")} built from this`,
  cited_replies: (n) => `${plural(n, "reply", "replies")} that cite this`,
  memories: (n) => `${plural(n, "memory", "memories")} from this conversation`,
};

export const FORGET_COPY: Copy = KEPT_COPY;

export function describeCounts(counts: Record<string, number>, copy: Copy): string[] {
  return Object.entries(counts)
    .filter(([key, n]) => n > 0 && key in copy)
    .map(([key, n]) => copy[key](n));
}
