import { readFile, writeFile, readdir } from "node:fs/promises";
import { join, resolve } from "node:path";
import crypto from "node:crypto";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { listPackage } = require("@electron/asar");

const release = resolve("release");
const forbidden = /(?:^|[\\/])(?:postgres-data|managed|Local Storage|Session Storage|IndexedDB|private-settings\.bin|PG_VERSION|local-data\.json)(?:$|[\\/])|\.(?:vistora|dump)$/i;
const resources = join(release, "win-unpacked", "resources");
const packagedFiles = [...await readdir(resources, { recursive: true }),
  ...listPackage(join(resources, "app.asar"))];
if (packagedFiles.some(name => forbidden.test(name))) {
  throw new Error("User database, browser history or backup found in release resources; do not distribute");
}
const pkg = JSON.parse(await readFile("package.json", "utf8"));
const files = (await readdir(release)).filter((name) => name.endsWith(".exe") && name.includes(pkg.version));
if (files.length !== 2) throw new Error("Expected portable and installer artifacts");
const configuredSecrets = [];
try {
  const env = await readFile(resolve("../../.data/dev/local.env"), "utf8");
  for (const line of env.split(/\r?\n/)) {
    const match = line.match(/^([A-Z_]+)=(.*)$/);
    if (match && /(?:KEY|TOKEN|PASSWORD)$/.test(match[1]) && match[2].length >= 16) configuredSecrets.push(Buffer.from(match[2]));
  }
} catch (error) { if (error.code !== "ENOENT") throw error; }
const asar = await readFile(join(release, "win-unpacked", "resources", "app.asar"));
const backend = await readFile(join(release, "win-unpacked", "resources", "backend", "vistora-backend.exe"));
if (configuredSecrets.some((secret) => asar.includes(secret) || backend.includes(secret))) {
  throw new Error("Configured private credentials found in an artifact; do not distribute");
}
const manifest = [];
for (const name of files.sort()) {
  const bytes = await readFile(join(release, name));
  manifest.push({ file: name, bytes: bytes.length, sha256: crypto.createHash("sha256").update(bytes).digest("hex") });
}
await writeFile(join(release, "SHA256SUMS.txt"), manifest.map((item) => `${item.sha256}  ${item.file}`).join("\n") + "\n");
await writeFile(join(release, "release-manifest.json"), JSON.stringify({ version: pkg.version,
  createdAt: new Date().toISOString(), distribution: "invitation-only beta candidate",
  knownCredentialScan: "passed", userDataFileScan: "passed", artifacts: manifest,
  remainingGates: ["Live vendor success requires a user-configured compatible API with valid credentials", "Independent clean Windows install/upgrade check", "Commercial code signing not configured"] }, null, 2));
console.log(JSON.stringify({ version: pkg.version, knownCredentialScan: "passed", artifacts: manifest }));
