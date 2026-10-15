import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  build: {
    rolldownOptions: {
      input: {
        app: fileURLToPath(new URL("./index.html", import.meta.url)),
        auth: fileURLToPath(new URL("./auth.html", import.meta.url)),
      },
    },
  },
  server: {
    proxy: {
      "/api": {
        target: process.env.UI_API_PROXY ?? "http://127.0.0.1:8080",
        changeOrigin: true,
        secure: true,
      },
    },
  },
});
