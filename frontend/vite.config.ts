import { defineConfig } from "vite";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { execFileSync } from "node:child_process";

function buildVersion(): string {
  const supplied = process.env.APP_BUILD_VERSION;
  if (supplied !== undefined) {
    if (!/^[1-9]\d*-[a-f0-9]{12}(?:-dirty)?$/.test(supplied)) {
      throw new Error("APP_BUILD_VERSION must be <commit-count>-<12-character-git-revision> with an optional -dirty suffix.");
    }
    return supplied;
  }
  const git = (...args: string[]) => execFileSync("git", args, { encoding: "utf8" }).trim();
  if (git("rev-parse", "--is-shallow-repository") === "true") {
    throw new Error("Build numbering requires full Git history. Run git fetch --unshallow before building.");
  }
  const count = git("rev-list", "--count", "HEAD");
  const revision = git("rev-parse", "--short=12", "HEAD");
  const dirty = git("status", "--porcelain") ? "-dirty" : "";
  return `${count}-${revision}${dirty}`;
}

export default defineConfig({
  define: { __APP_VERSION__: JSON.stringify(buildVersion()) },
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
