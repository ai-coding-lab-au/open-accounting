import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  resolve: {
    extensions: [".mjs", ".tsx", ".ts", ".jsx", ".js", ".json"],
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    // WSL2 9p filesystem inotify is unreliable — use polling so HMR
    // catches every save instead of silently dropping changes.
    watch: {
      usePolling: true,
      interval: 500,
    },
    proxy: {
      // VITE_E2E_BACKEND_URL lets the e2e bootstrap point Vite at the
      // throwaway backend on :8765 without editing this file. Devs
      // running `npm run dev` against their normal stack hit :8787 —
      // the port start.ps1 / seed-demo / reset-demo all use.
      "/api": {
        target: process.env.VITE_E2E_BACKEND_URL ?? "http://127.0.0.1:8787",
        changeOrigin: false,
      },
    },
  },
});
