import type { StorybookConfig } from "@storybook/react-vite";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";

const root = fileURLToPath(new URL("../", import.meta.url));
const dependencies = Object.keys(JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).dependencies);
const config: StorybookConfig = {
  stories: ["../stories/**/*.stories.tsx"],
  addons: ["@storybook/addon-docs"],
  framework: "@storybook/react-vite",
  core: { disableTelemetry: true },
  typescript: { reactDocgen: false },
  async viteFinal(config) {
    const { mergeConfig } = await import("vite");
    return mergeConfig(config, {
      envDir: root,
      resolve: {
        // Source components live outside this app. Resolve their dependencies here,
        // including one React instance, even when ui/node_modules is absent.
        alias: dependencies.map((name) => ({
          find: new RegExp("^" + name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "(?=/|$)"),
          replacement: root + "node_modules/" + name,
        })),
        dedupe: ["react", "react-dom"],
      },
      server: { fs: { allow: [fileURLToPath(new URL("../../", import.meta.url))] } },
    });
  },
};
export default config;
