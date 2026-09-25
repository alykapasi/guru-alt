import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  test: {
    environment: "jsdom",
    // Testing Library registers its between-test DOM cleanup through the global afterEach;
    // without this each test inherits the previous one's markup and assertions find it.
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // Vite loads `.env.local` for the test run too, so without this the suite's result depends
    // on whether the developer happens to have configured Clerk: `clerkEnabled` is derived from
    // this key, `RequireLearner` mounts a Clerk component when it is set, and that file's mock
    // throws on any Clerk hook by design. CI has no key and stayed green while a machine that
    // had one went red — the same shape as a test that only passes where docker compose is up.
    //
    // The suite therefore always runs the keyless build. A test that wants the other one says
    // so out loud by mocking `src/auth/mode`, as `src/pages/AuthPages.test.tsx` does, rather
    // than by inheriting whatever is in someone's env file.
    env: { VITE_CLERK_PUBLISHABLE_KEY: "" },
  },
});
