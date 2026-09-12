import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";

/** Newest-first, per the backend's ordering (app/services/chat.py::list_conversations). */
export function useConversations() {
  return useQuery({
    queryKey: ["conversations"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/conversations");
      if (error) throw error;
      return data;
    },
  });
}

/** One assessment item by id — used to restore the practice item a paused session is on after
 * a reload, where the item is known only as `conversation.active_item_id` (S52). */
export function useItem(itemId: string | null | undefined) {
  return useQuery({
    queryKey: ["item", itemId],
    enabled: !!itemId,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/items/{item_id}", {
        params: { path: { item_id: itemId! } },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function useCreateConversation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (
      scope: {
        title?: string;
        kind?: "chat" | "session";
        subject_id?: string;
        source_ids?: string[];
      } = {},
    ) => {
      const { data, error } = await api.POST("/api/v1/conversations", {
        body: { ...scope, kind: scope.kind ?? "chat" },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

/** For the "New chat" scope picker — subject or "General". */
export function useSubjects() {
  return useQuery({
    queryKey: ["subjects"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/subjects");
      if (error) throw error;
      return data;
    },
  });
}

/** For the "New chat" scope picker's per-source narrowing, once a subject is chosen. */
export function useSources(subjectId: string | undefined) {
  return useQuery({
    queryKey: ["sources", subjectId],
    enabled: subjectId !== undefined,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/sources", {
        params: { query: { subject_id: subjectId } },
      });
      if (error) throw error;
      return data;
    },
  });
}

/** For the Uploads page — every source, or those scoped to one subject; unlike useSources
 * (gated behind a chosen subject for the New Chat picker), this is always enabled. */
export function useAllSources(subjectId: string | undefined) {
  return useQuery({
    queryKey: ["sources", "all", subjectId],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/sources", {
        params: { query: { subject_id: subjectId } },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function useUploadSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ file, subjectId }: { file: File; subjectId?: string }) => {
      const form = new FormData();
      form.append("file", file);
      if (subjectId) form.append("subject_id", subjectId);
      const { data, error } = await api.POST("/api/v1/sources/upload", {
        // openapi-typescript models multipart fields as strings; FormData is the real wire
        // shape and openapi-fetch passes it through untouched (see defaultBodySerializer).
        body: form as unknown as { file: string; subject_id?: string | null },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sources"] });
    },
  });
}

export function useLinkSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ url, subjectId }: { url: string; subjectId?: string }) => {
      const { data, error } = await api.POST("/api/v1/sources/link", {
        body: { url, subject_id: subjectId },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sources"] });
    },
  });
}

/** For the citation pane — the cited chunk's source (filename/URL), alongside useChunk. */
export function useSource(sourceId: string | undefined) {
  return useQuery({
    queryKey: ["source", sourceId],
    enabled: sourceId !== undefined,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/sources/{source_id}", {
        params: { path: { source_id: sourceId! } },
      });
      if (error) throw error;
      return data;
    },
  });
}

/** For the citation pane's click-through — fetched on demand, not preloaded. */
export function useChunk(chunkId: string | undefined) {
  return useQuery({
    queryKey: ["chunk", chunkId],
    enabled: chunkId !== undefined,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/chunks/{chunk_id}", {
        params: { path: { chunk_id: chunkId! } },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function useRenameConversation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, title }: { id: string; title: string }) => {
      const { data, error } = await api.PATCH("/api/v1/conversations/{conversation_id}", {
        params: { path: { conversation_id: id } },
        body: { title },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

export function useDeleteConversation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      const { error } = await api.DELETE("/api/v1/conversations/{conversation_id}", {
        params: { path: { conversation_id: id } },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

/** A single KC's name/description — for resolving a lesson-plan step's `kc_id` to a label. */
export function useKC(kcId: string | undefined) {
  return useQuery({
    queryKey: ["kc", kcId],
    enabled: kcId !== undefined,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/kcs/{kc_id}", {
        params: { path: { kc_id: kcId! } },
      });
      if (error) throw error;
      return data;
    },
  });
}

/** Null (not an error) when the learner has no plan yet for this subject — the backend 404s to
 * distinguish "never generated" from "empty plan," which the Lessons page treats as a CTA. */
export function useLessonPlan(subjectId: string | undefined) {
  return useQuery({
    queryKey: ["lesson-plan", subjectId],
    enabled: subjectId !== undefined,
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/v1/subjects/{subject_id}/lesson-plan", {
        params: { path: { subject_id: subjectId! } },
      });
      if (response.status === 404) return null;
      if (error) throw error;
      return data;
    },
  });
}

export function useGenerateLessonPlan(subjectId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (goal: string | null) => {
      const { data, error } = await api.POST("/api/v1/subjects/{subject_id}/lesson-plan", {
        params: { path: { subject_id: subjectId! } },
        body: { goal },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(["lesson-plan", subjectId], data);
    },
  });
}

export function usePlacementPrompt(subjectId: string | undefined) {
  return useQuery({
    queryKey: ["placement-prompt", subjectId],
    enabled: subjectId !== undefined,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/subjects/{subject_id}/placement/prompt", {
        params: { path: { subject_id: subjectId! } },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function useSubmitPlacement(subjectId: string | undefined) {
  return useMutation({
    mutationFn: async (background: string) => {
      const { data, error } = await api.POST("/api/v1/subjects/{subject_id}/placement", {
        params: { path: { subject_id: subjectId! } },
        body: { background },
      });
      if (error) throw error;
      return data;
    },
  });
}

/** Chronological, per the backend's ordering (app/services/chat.py::list_messages). */
export function useMessages(conversationId: string | undefined) {
  return useQuery({
    queryKey: ["messages", conversationId],
    enabled: conversationId !== undefined,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/conversations/{conversation_id}/messages", {
        params: { path: { conversation_id: conversationId! } },
      });
      if (error) throw error;
      return data;
    },
  });
}

// --- Dashboard: mastery, activity, profile, reviews-due --------------------

export function useSubjectMastery(subjectId: string | undefined) {
  return useQuery({
    queryKey: ["mastery", subjectId],
    enabled: subjectId !== undefined,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/subjects/{subject_id}/mastery", {
        params: { path: { subject_id: subjectId! } },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function useActivity() {
  return useQuery({
    queryKey: ["activity"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/activity");
      if (error) throw error;
      return data;
    },
  });
}

export function useProfile() {
  return useQuery({
    queryKey: ["profile"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/profile");
      if (error) throw error;
      return data;
    },
  });
}

export function useRefreshProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/v1/profile/refresh");
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(["profile"], data);
    },
  });
}

export function useResetDimension() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (key: string) => {
      const { error } = await api.POST("/api/v1/profile/{key}/reset", {
        params: { path: { key } },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["profile"] });
    },
  });
}

/** Soonest-due-first, across every subject (see ROADMAP: FSRS review queue). */
export function useReviewsDue() {
  return useQuery({
    queryKey: ["reviews-due"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/reviews/due");
      if (error) throw error;
      return data;
    },
  });
}

// --- Memory: what the system believes about the learner (S16) ---------------

/** What is remembered about the learner — current entries only; superseded and deleted rows
 * exist to keep extraction well-behaved, not to be shown back. */
export function useMemories() {
  return useQuery({
    queryKey: ["memories"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/memory");
      if (error) throw error;
      return data;
    },
  });
}

/** Say what is actually true. The server supersedes rather than overwrites, so the corrected
 * entry comes back with a new id — refetch rather than patching the cache in place. */
export function useCorrectMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, content }: { id: string; content: string }) => {
      const { data, error } = await api.PATCH("/api/v1/memory/{memory_id}", {
        params: { path: { memory_id: id } },
        body: { content },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["memories"] });
    },
  });
}

export function useForgetMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      const { error } = await api.DELETE("/api/v1/memory/{memory_id}", {
        params: { path: { memory_id: id } },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["memories"] });
    },
  });
}

export function useForgetAllMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { error } = await api.DELETE("/api/v1/memory");
      if (error) throw error;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["memories"] });
    },
  });
}
