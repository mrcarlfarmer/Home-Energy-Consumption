import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import { resolve } from "node:path";

const local = resolve("..", ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const python = existsSync(local) ? `"${local}"` : "python";

export default defineConfig({
  testDir: "./e2e",
  workers: 1,
  use: { baseURL: "http://127.0.0.1:8765", browserName: "chromium" },
  webServer: [{
    command: `${python} "${resolve("..", "tests", "serve.py")}"`,
    url: "http://127.0.0.1:8765/healthz",
    timeout: 30000,
    reuseExistingServer: false,
  }, {
    command: `${python} "${resolve("..", "tests", "serve.py")}" --no-login --port 8766`,
    url: "http://127.0.0.1:8766/healthz",
    timeout: 30000,
    reuseExistingServer: false,
  }],
});
