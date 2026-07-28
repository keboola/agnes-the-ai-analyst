import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Agnes serves data apps under /apps/<slug>/ (path-prefix ingress mode) or a
// dedicated subdomain — either way the app never knows its own prefix ahead
// of time, so assets must resolve relative to wherever the page was loaded
// from rather than an absolute "/" base.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
  },
  server: {
    proxy: {
      "/api": "http://localhost:3000",
    },
  },
});
