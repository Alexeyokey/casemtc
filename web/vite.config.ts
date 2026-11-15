/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Dev-сервер проксирует /api на backend (uvicorn :8080), поэтому в браузере
// нет CORS и адрес API не зашит в код: см. .env.example.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "VITE_");
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: { "/api": env.VITE_DEV_PROXY_TARGET || "http://127.0.0.1:8080" },
    },
    build: { outDir: "dist", sourcemap: false },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      css: { modules: { classNameStrategy: "non-scoped" } },
    },
  };
});
