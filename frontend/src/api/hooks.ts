import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, apiFetch } from "./client";

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

/** A flashcard's reverse face, fetched only when the learner asks to see it (S54) — see
 * `app/api/v1/assessment.py`'s `reveal_item`. A plain function rather than a `useMutation`
 * hook: `FlashcardPanel` calls it directly from a click handler and there is no cached query
 * for a successful reveal to invalidate. Goes through `apiFetch` rather than the typed client
 * because it is invoked outside a component, as the default for `FlashcardPanel`'s injectable
 * `reveal` prop. */
export async function defaultReveal(itemId: string): Promise<string> {
  const res = await apiFetch(`/api/v1/items/${itemId}/reveal`, { method: "POST" });
  if (!res.ok) {
    throw new Error(`reveal failed: ${res.status} ${res.statusText}`);
  }
  const data = (await res.json()) as { back: string };
  return data.back;
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

/** How often to re-ask while a document is still being ingested. */
const INGESTION_POLL_MS = 2000;

/** Whether anything in this list is still being worked on.
 *
 * Exported so the rule can be tested without a rendered page, and named rather than inlined
 * because it is the condition that decides whether the library is a live view or a snapshot.
 */
export function stillIngesting(sources: { status: string }[] | undefined): boolean {
  return (sources ?? []).some((s) => s.status === "pending" || s.status === "processing");
}

/** For the Uploads page and the subject wizard's materials step — every source, or those
 * scoped to one subject; unlike useSources (gated behind a chosen subject for the New Chat
 * picker), this is always enabled.
 *
 * It polls while anything is in flight, and that is not a refinement. Uploading returns as
 * soon as the bytes are stored; extraction, chunking and embedding happen in a queued job
 * afterwards. So the status this first renders is always "pending", and without a refetch it
 * stays "pending" for the rest of the session — no error, no spinner resolving, nothing
 * moving — and the only way a learner can find out their document is ready is to reload the
 * page. The interval stops as soon as nothing is in flight, so a settled library costs
 * nothing.
 */
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
    refetchInterval: (query) => (stillIngesting(query.state.data) ? INGESTION_POLL_MS : false),
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

/** Switches how much the planner may decide for the learner on its own (V07/S11): guided takes
 * detours on its own, exploration only offers them. Same cache-write as useGenerateLessonPlan —
 * the response is a full plan, so there is nothing to invalidate. */
export function useSetGuidance(subjectId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (guidance: "guided" | "exploration") => {
      const { data, error } = await api.PATCH(
        "/api/v1/subjects/{subject_id}/lesson-plan/guidance",
        {
          params: { path: { subject_id: subjectId! } },
          body: { guidance },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(["lesson-plan", subjectId], data);
    },
  });
}

/** A learner's answer to an offered detour — take it or skip it (S11). A 409 means the detour
 * closed before the decision landed (the blocker resolved itself, say); there is nothing to
 * patch onto a plan that no longer has that offer. React Query does not refetch after a failed
 * mutation, so the plan is invalidated on error — otherwise the stale offer stays on screen
 * with buttons that can only fail again. */
export function useDecideDetour(subjectId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      prereqKcId,
      decision,
    }: {
      prereqKcId: string;
      decision: "accept" | "skip";
    }) => {
      const { data, error } = await api.POST(
        "/api/v1/subjects/{subject_id}/lesson-plan/detours/{prereq_kc_id}",
        {
          params: { path: { subject_id: subjectId!, prereq_kc_id: prereqKcId } },
          body: { decision },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(["lesson-plan", subjectId], data);
    },
    onError: () => {
      queryClient.invalidateQueries({ queryKey: ["lesson-plan", subjectId] });
    },
  });
}

/** Prerequisite cycles in a subject the caller owns, and the edge the planner ignores to
 * break each one (S23). */
export function usePrerequisiteConflicts(subjectId: string | undefined) {
  return useQuery({
    queryKey: ["prerequisite-conflicts", subjectId],
    enabled: !!subjectId,
    queryFn: async () => {
      const { data, error } = await api.GET(
        "/api/v1/subjects/{subject_id}/prerequisite-conflicts",
        {
          params: { path: { subject_id: subjectId! } },
        },
      );
      if (error) throw error;
      return data;
    },
  });
}

/** Remove one prerequisite edge — the repair a conflict report offers (S23). The plan is
 * refetched too: the order it was built from just changed. */
export function useRemovePrerequisite(subjectId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ kcId, prereqKcId }: { kcId: string; prereqKcId: string }) => {
      const { error } = await api.DELETE("/api/v1/kcs/{kc_id}/prerequisites/{prereq_kc_id}", {
        params: { path: { kc_id: kcId, prereq_kc_id: prereqKcId } },
      });
      if (error) throw error;
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["prerequisite-conflicts", subjectId] });
      queryClient.invalidateQueries({ queryKey: ["lesson-plan", subjectId] });
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

/** Pause, resume, or skip guided practice explicitly (S52) — the frontend's own controls, as
 * opposed to the intent gate that infers a pause/skip from an ordinary chat message. A 409
 * means the control no longer fits the conversation's current state (e.g. a turn started
 * streaming since the button was drawn); there is nothing local to patch onto in that case.
 * React Query does not refetch after a failed mutation, so the conversation and its transcript
 * are invalidated either way (onSettled) — the controls redraw from the server's actual state
 * instead of staying stale. Same reasoning as useDecideDetour's 409. */
export function usePracticeAction(conversationId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (action: "pause" | "resume" | "skip") => {
      const { data, error } = await api.POST("/api/v1/conversations/{conversation_id}/practice", {
        params: { path: { conversation_id: conversationId! } },
        body: { action },
      });
      if (error) throw error;
      return data;
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      queryClient.invalidateQueries({ queryKey: ["messages", conversationId] });
    },
  });
}

/** Chronological, per the backend's ordering (app/services/chat.py::list_messages). */
/** One conversation's transcript, newest page first, paging backwards on demand (S62).
 *
 * Infinite rather than a single read because the endpoint is now bounded: asking for a bigger
 * `limit` each time someone wants older messages would re-read everything already on screen,
 * which is most of the cost this bound exists to remove. Each page is a cursor step instead.
 *
 * Pages arrive newest-block-first and are each internally oldest-first, so a consumer wanting
 * chronological order reverses the page list before flattening. */
export function useMessages(conversationId: string | undefined) {
  return useInfiniteQuery({
    queryKey: ["messages", conversationId],
    enabled: conversationId !== undefined,
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => {
      const { data, error } = await api.GET("/api/v1/conversations/{conversation_id}/messages", {
        params: {
          path: { conversation_id: conversationId! },
          query: pageParam ? { before: pageParam } : {},
        },
      });
      if (error) throw error;
      return data;
    },
    // The oldest message on the oldest page we hold is the next cursor. `has_more` is the
    // server's answer, so an empty page can never loop.
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.messages[0]?.id : undefined),
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
