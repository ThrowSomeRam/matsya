import { resolve } from "node:path";
import { defineConfig } from "vite";

// Two pages: the owner view (/) and the evidence page (/evidence/). /api goes to the owner-view API
// (platform/src/matsya_platform/api) in dev and preview; in compose, nginx does the same.
const api = process.env.MATSYA_API_URL ?? "http://127.0.0.1:8000";
const proxy = { "/api": { target: api, changeOrigin: true, rewrite: (p: string) => p.replace(/^\/api/, "") } };

export default defineConfig({
  build: {
    rollupOptions: {
      input: { main: resolve(__dirname, "index.html"), evidence: resolve(__dirname, "evidence/index.html") },
    },
  },
  server: { proxy },
  preview: { proxy },
});
