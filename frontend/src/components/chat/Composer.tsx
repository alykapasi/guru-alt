import { useState } from "react";
import { ArrowUp, Bot, MessageCircle } from "lucide-react";

export function Composer({
  disabled,
  onSend,
  fixedMode,
  placeholder = "Message Guru…",
}: {
  disabled: boolean;
  onSend: (content: string, mode: "chat" | "agentic" | "workflow") => void;
  /** Skips the mode toggle and always sends this mode — for single-purpose composers like the
   * guided-practice session, where "chat vs. agentic" isn't a choice the learner makes. */
  fixedMode?: "chat" | "agentic" | "workflow";
  placeholder?: string;
}) {
  const [content, setContent] = useState("");
  const [mode, setMode] = useState<"chat" | "agentic">("chat");

  function submit() {
    const trimmed = content.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed, fixedMode ?? mode);
    setContent("");
  }

  return (
    <div className="border-base-300 bg-base-100 border-t px-6 py-4">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-2">
        {!fixedMode && (
          <div className="flex items-center gap-1">
            <button
              onClick={() => setMode("chat")}
              className={`text-caption flex items-center gap-1.5 rounded-field px-2.5 py-1 transition-colors ${
                mode === "chat"
                  ? "bg-primary/10 text-primary"
                  : "text-base-content/50 hover:bg-base-200"
              }`}
            >
              <MessageCircle size={14} />
              Chat
            </button>
            <button
              onClick={() => setMode("agentic")}
              className={`text-caption flex items-center gap-1.5 rounded-field px-2.5 py-1 transition-colors ${
                mode === "agentic"
                  ? "bg-primary/10 text-primary"
                  : "text-base-content/50 hover:bg-base-200"
              }`}
            >
              <Bot size={14} />
              Agentic
            </button>
          </div>
        )}
        <div className="flex items-end gap-2">
          <textarea
            name="message"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            disabled={disabled}
            rows={1}
            placeholder={placeholder}
            className="textarea text-body max-h-40 flex-1 resize-none"
          />
          <button
            onClick={submit}
            disabled={disabled || !content.trim()}
            className="btn btn-primary btn-square"
            aria-label="Send"
          >
            <ArrowUp size={18} />
          </button>
        </div>
      </div>
    </div>
  );
}
