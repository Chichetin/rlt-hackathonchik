import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Dev: `npm run dev` → http://127.0.0.1:5173, /api проксируется на uvicorn (make api, порт 8000).
export default defineConfig({
  plugins: [react()],
  server: { host: "127.0.0.1", proxy: { "/api": `http://127.0.0.1:${process.env.API_PORT ?? 8000}` } },
  test: { environment: "jsdom", globals: true, setupFiles: ["./src/test-setup.ts"], include: ["src/**/*.test.tsx", "src/**/*.test.ts"] },
});
