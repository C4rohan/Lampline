import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";

// The engine serves this build from /static (index.html at /). `npm run dev` proxies the
// API to an engine on :7860.
export default defineConfig({
  base: "/static/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: {
    outDir: "../src/vpengine/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // Fixed file names (no content hashes): two branches that both rebuild the UI then change the same files,
        // so git shows a conflict to rebuild, instead of quietly keeping both builds. The engine serves them with
        // Cache-Control: no-cache, so browsers still pick up a new version.
        entryFileNames: "assets/[name].js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
        // The builder's canvas libraries load only on the builder page.
        manualChunks: (id: string) =>
          /node_modules\/(@xyflow|@dagrejs)\//.test(id) ? "flow" : undefined,
      },
    },
  },
  server: { proxy: { "/v1": "http://127.0.0.1:7860" } },
});
