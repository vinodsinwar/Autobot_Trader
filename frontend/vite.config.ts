import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "../backend/static", emptyOutDir: true },
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        ws: true,
      },
    },
  },
});
