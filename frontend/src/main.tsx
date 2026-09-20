import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ClerkProvider } from "@clerk/react";
import "./index.css";
import App from "./App.tsx";
import { CLERK_PUBLISHABLE_KEY, clerkEnabled } from "./auth/mode";

const queryClient = new QueryClient();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      {/* Mounted only with a key: `ClerkProvider` throws without one, so a keyless build
          renders the app straight through and never enters Clerk's tree at all (S21). */}
      {clerkEnabled ? (
        <ClerkProvider publishableKey={CLERK_PUBLISHABLE_KEY!} afterSignOutUrl="/signin">
          <App />
        </ClerkProvider>
      ) : (
        <App />
      )}
    </QueryClientProvider>
  </StrictMode>,
);
