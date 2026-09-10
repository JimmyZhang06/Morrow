import { spawn } from "node:child_process";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const child = spawn(process.execPath, [require.resolve("electron-builder/cli.js"), "--win", ...process.argv.slice(2)], {
  stdio: "inherit", windowsHide: true,
  env: { ...process.env, ELECTRON_BUILDER_COMPRESSION_LEVEL: process.env.ELECTRON_BUILDER_COMPRESSION_LEVEL || "3" },
});
child.once("error", () => process.exit(1));
child.once("exit", (code) => process.exit(code === 0 ? 0 : 1));
