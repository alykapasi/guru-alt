import { MessageSquare } from "lucide-react";
import { PlaceholderPage } from "../components/PlaceholderPage";
import { useConversations } from "../api/hooks";

export function Chat() {
  const { data, isLoading, isError } = useConversations();

  return (
    <>
      <PlaceholderPage
        icon={MessageSquare}
        title="Chat"
        description="The streaming tutor conversation — chat, agentic tool use, and guided-practice workflow modes — lands in the next slice."
      />
      <p className="text-caption text-base-content/50">
        {isLoading && "Checking the API…"}
        {isError && "Couldn't reach the API — is the backend running?"}
        {data &&
          `Connected — ${data.length} existing conversation${data.length === 1 ? "" : "s"} on the backend.`}
      </p>
    </>
  );
}
