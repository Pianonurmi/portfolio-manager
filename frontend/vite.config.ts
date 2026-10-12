import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["icon.svg", "apple-touch-icon.png"],
      manifest: {
        name: "Portfolio Manager",
        short_name: "Portfolio",
        description: "Household finances, investments and planning",
        theme_color: "#0c0e12",
        background_color: "#0c0e12",
        display: "standalone",
        start_url: "/",
        icons: [
          { src: "icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "icon-512.png", sizes: "512x512", type: "image/png" },
          { src: "icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
          { src: "icon.svg", sizes: "any", type: "image/svg+xml" },
        ],
      },
      workbox: {
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [/^\/api\//, /^\/healthz/],
        runtimeCaching: [
          {
            // Last-known dashboard data for offline reading (UX-07). Auth endpoints are never cached.
            urlPattern: ({ url }) =>
              url.pathname.startsWith("/api/") && !url.pathname.startsWith("/api/auth") && url.pathname !== "/api/jobs",
            handler: "NetworkFirst",
            method: "GET",
            options: { cacheName: "api-last-known", networkTimeoutSeconds: 4, expiration: { maxEntries: 200, maxAgeSeconds: 7 * 86400 } },
          },
        ],
      },
    }),
  ],
  server: {
    port: 5173,
    proxy: { "/api": process.env.PM_API ?? "http://localhost:8000", "/healthz": process.env.PM_API ?? "http://localhost:8000" },
  },
  build: { target: "es2022", sourcemap: false, chunkSizeWarningLimit: 600 },
  test: { environment: "jsdom", globals: false },
});
