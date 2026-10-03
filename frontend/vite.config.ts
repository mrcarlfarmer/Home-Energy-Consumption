import { defineConfig } from "vite";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

export default defineConfig({
  build: { outDir: "../backend/app/static", emptyOutDir: true },
  plugins: [{
    name: "retain-uplot-license",
    generateBundle() {
      this.emitFile({
        type: "asset",
        fileName: "assets/uplot-LICENSE.txt",
        source: readFileSync(resolve("node_modules/uplot/LICENSE"), "utf8"),
      });
    },
  }],
});
