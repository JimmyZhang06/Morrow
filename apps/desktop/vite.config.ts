import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/__vistora_api": {
        target: `http://127.0.0.1:${process.env.LOCAL_API_PORT || "8000"}`,
        changeOrigin: true,
        rewrite: (requestPath) => requestPath.replace(/^\/__vistora_api/, ""),
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
