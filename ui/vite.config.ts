import { defineConfig } from "vite";

export default defineConfig({
  base: "/ui/",
  build: { outDir: "../core_agent/ui_dist", emptyOutDir: true, sourcemap: false },
});
