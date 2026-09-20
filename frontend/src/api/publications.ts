import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";

/** Publication: asking for a subject to be shared, and reviewing those requests (S25b).
 *
 * Nothing here polls. A publication changes when somebody acts on it — the author asks or
 * cancels, a reviewer approves or rejects — and every one of those happens through a mutation
 * below, which invalidates what it changed. A queue that refreshed under a reviewer mid-read
 * would be harder to work through, not more current. */

export type PublicationStatus = "pending" | "approved" | "rejected" | "cancelled";

export interface Publication {
  id: string;
  status: PublicationStatus;
  author_note: string | null;
  review_note: string | null;
  reviewed_at: string | null;
  created_at: string;
  published_subject_id: string | null;
}

export interface SnapshotItem {
  id: string;
  item_type: string;
  stem: string;
  answer_key: Record<string, unknown> | null;
  difficulty: number;
  rubric_id: string | null;
  origin: string;
  kc_weights: { kc_id: string; weight: number }[];
}

/** The frozen graph a reviewer judges.
 *
 * Cast from the generated types rather than described by them, and deliberately so. The column
 * is free-form JSONB because it is an *archival* record — "what was approved", kept after the
 * subject it came from has moved on or gone. Describing it strictly server-side would mean one
 * old row whose shape has drifted takes down the whole review queue on read, instead of
 * rendering oddly on its own. So the shape is asserted here, at the one place that renders it. */
export interface Snapshot {
  subject: { name: string; description: string | null };
  topics: { id: string; slug: string; name: string; description: string | null }[];
  kcs: { id: string; topic_id: string; slug: string; name: string; description: string | null }[];
  edges: { prereq_kc_id: string; kc_id: string; weight: number }[];
  items: SnapshotItem[];
  rubrics: { id: string; kc_id: string; name: string | null; criteria: Record<string, unknown> }[];
}

export interface PublicationReview extends Omit<Publication, "author_note"> {
  author_handle: string | null;
  author_note: string | null;
  reviewer_handle: string | null;
  snapshot: Snapshot;
}

/** The author's view of what they have asked for on one subject. */
export function usePublications(subjectId: string | null) {
  return useQuery({
    queryKey: ["publications", subjectId],
    enabled: subjectId !== null,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/subjects/{subject_id}/publications", {
        params: { path: { subject_id: subjectId as string } },
      });
      if (error) throw error;
      return data.publications as Publication[];
    },
  });
}

export function useRequestPublication(subjectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (note: string | null) => {
      const { data, error } = await api.POST("/api/v1/subjects/{subject_id}/publications", {
        params: { path: { subject_id: subjectId } },
        body: { note },
      });
      if (error) throw error;
      return data as Publication;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["publications", subjectId] });
    },
  });
}

export function useCancelPublication(subjectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (publicationId: string) => {
      const { data, error } = await api.POST("/api/v1/publications/{publication_id}/cancel", {
        params: { path: { publication_id: publicationId } },
      });
      if (error) throw error;
      return data as Publication;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["publications", subjectId] });
    },
  });
}

// --- the reviewer's side -------------------------------------------------------------------

export function useReviewQueue(status: PublicationStatus | null = "pending") {
  return useQuery({
    queryKey: ["admin", "publications", status],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/admin/publications", {
        params: { query: status ? { status } : {} },
      });
      if (error) throw error;
      return data.publications as unknown as PublicationReview[];
    },
  });
}

export function useApprovePublication() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      publicationId,
      excludedItemIds,
      note,
    }: {
      publicationId: string;
      excludedItemIds: string[];
      note: string | null;
    }) => {
      const { data, error } = await api.POST(
        "/api/v1/admin/publications/{publication_id}/approve",
        {
          params: { path: { publication_id: publicationId } },
          body: { excluded_item_ids: excludedItemIds, note },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "publications"] });
      // The shared library just gained a subject, so every learner's catalog changed.
      queryClient.invalidateQueries({ queryKey: ["subjects"] });
    },
  });
}

export function useRejectPublication() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ publicationId, note }: { publicationId: string; note: string }) => {
      const { data, error } = await api.POST("/api/v1/admin/publications/{publication_id}/reject", {
        params: { path: { publication_id: publicationId } },
        body: { note },
      });
      if (error) throw error;
      return data as unknown as PublicationReview;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "publications"] });
    },
  });
}

export function useWithdrawSubject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ subjectId, reason }: { subjectId: string; reason: string }) => {
      const { data, error } = await api.POST("/api/v1/admin/subjects/{subject_id}/withdraw", {
        params: { path: { subject_id: subjectId } },
        body: { reason },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin", "publications"] });
      queryClient.invalidateQueries({ queryKey: ["subjects"] });
    },
  });
}
