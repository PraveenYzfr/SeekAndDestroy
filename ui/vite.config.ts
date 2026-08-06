import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// SeekAndDestroy UI - proxies /api to the ASP.NET Core gateway in dev so the
// browser never needs CORS configuration beyond what Program.cs already sets.
export default defineConfig({
  plugins: [react()],
  resolve: {
    // Must mirror tsconfig.json's "paths" - tsc only type-checks the alias,
    // it doesn't resolve it at bundle time; Vite/Rollup need their own copy.
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_GATEWAY_URL ?? "http://127.0.0.1:5090",
        changeOrigin: true,
      },
    },
  },
});
