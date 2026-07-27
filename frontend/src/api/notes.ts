import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API_BASE_URL } from "./client";

// ============================================================================
// Types (mirror app/schemas/note.py)
// ============================================================================

export type NoteFormat = "outline" | "narrative" | "mnemonic" | "worked_examples";

export interface NoteRead {
  topic_id: string;
  content_md: string | null;
  format: NoteFormat | null;
  effective_format: NoteFormat;
  stale: boolean;
  revision_ordinal: number | null;
  updated_at: string | null;
}

export interface NoteIndexEntry {
  topic_id: string;
  topic_name: string;
  has_note: boolean;
  stale: boolean;
  updated_at: string | null;
}

export interface NoteRevisionRead {
  ordinal: number;
  cause: "distill" | "learner_edit" | "restore";
  created_at: string;
}

export interface NoteRevisionSource {
  ordinal: number;
  content_md: string;
}

// ============================================================================
// Fetch helper (new endpoints aren't in the generated schema.d.ts — same
// raw-fetch approach as onboarding.ts)
// ============================================================================

/** Mirrors onboarding.ts's `.status`-attaching pattern, plus parses the FastAPI `detail` body
 * so callers (e.g. NoteView's edit-conflict banner) can surface the backend's own user-facing
 * message instead of a generic "request failed". */
async function jfetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE_URL}/api/v1${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
    const message =
      typeof body?.detail === "string"
        ? body.detail
        : `${init?.method ?? "GET"} ${path} failed: ${res.status}`;
    const error = new Error(message) as Error & { status?: number };
    error.status = res.status;
    throw error;
  }
  return (await res.json()) as T;
}

// ============================================================================
// Hooks
// ============================================================================

export function useNotesIndex(subjectId: string | null) {
  return useQuery({
    queryKey: ["notes", subjectId],
    queryFn: () => jfetch<NoteIndexEntry[]>(`/subjects/${subjectId}/notes`),
    enabled: subjectId !== null,
  });
}

export function useNote(topicId: string) {
  return useQuery({
    queryKey: ["note", topicId],
    queryFn: () => jfetch<NoteRead>(`/topics/${topicId}/note`),
  });
}

/** Shared: after any mutation the note, its history, and the index badges are all stale. */
function useInvalidateNote(topicId: string) {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: ["note", topicId] });
    void qc.invalidateQueries({ queryKey: ["note-revisions", topicId] });
    void qc.invalidateQueries({ queryKey: ["notes"] });
  };
}

export function useRefreshNote(topicId: string) {
  const invalidate = useInvalidateNote(topicId);
  return useMutation({
    mutationFn: () => jfetch<NoteRead>(`/topics/${topicId}/note/refresh`, { method: "POST" }),
    onSuccess: invalidate,
  });
}

export function useEditNote(topicId: string) {
  const invalidate = useInvalidateNote(topicId);
  return useMutation({
    mutationFn: (content_md: string) =>
      jfetch<NoteRead>(`/topics/${topicId}/note`, {
        method: "PUT",
        body: JSON.stringify({ content_md }),
      }),
    onSuccess: invalidate,
  });
}

export function useSetFormat(topicId: string) {
  const invalidate = useInvalidateNote(topicId);
  return useMutation({
    mutationFn: (format: NoteFormat | null) =>
      jfetch<NoteRead>(`/topics/${topicId}/note/format`, {
        method: "PATCH",
        body: JSON.stringify({ format }),
      }),
    onSuccess: invalidate,
  });
}

export function useRevisions(topicId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["note-revisions", topicId],
    queryFn: () => jfetch<NoteRevisionRead[]>(`/topics/${topicId}/note/revisions`),
    enabled,
  });
}

export function useRevisionSource(topicId: string, ordinal: number | null) {
  return useQuery({
    queryKey: ["note-revision-source", topicId, ordinal],
    queryFn: () => jfetch<NoteRevisionSource>(`/topics/${topicId}/note/revisions/${ordinal}`),
    enabled: ordinal !== null,
  });
}

export function useRestoreRevision(topicId: string) {
  const invalidate = useInvalidateNote(topicId);
  return useMutation({
    mutationFn: (ordinal: number) =>
      jfetch<NoteRead>(`/topics/${topicId}/note/revisions/${ordinal}/restore`, {
        method: "POST",
      }),
    onSuccess: invalidate,
  });
}
